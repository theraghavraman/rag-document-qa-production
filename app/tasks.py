"""Version-based ingestion with leases, idempotence, tombstones and recovery."""
import hashlib
import logging
import shutil
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import delete, or_, select, update
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from app.celery_app import celery_app
from app.config import get_settings
from app.database import SessionLocal
from app.models import AuditEvent, DocumentRecord, DocumentVersion, UsageEvent, SignupIntent, Invitation, RefreshToken, utcnow
from app.parsing import InvalidDocument, chunk_pages, load_pages
from app.storage import create_dirs, document_prefix, get_store, index_prefix, scan_file
from app.vector_index import upload_index, write_index

logger = logging.getLogger(__name__)
TRANSIENT = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)


def _claim(version_id: str, token: str) -> bool:
    s = get_settings()
    with SessionLocal.begin() as db:
        current = db.get(DocumentVersion, version_id)
        if not current:
            return False
        doc = db.scalar(select(DocumentRecord).where(DocumentRecord.id == current.document_id,
                                                     DocumentRecord.tenant_id == current.tenant_id))
        if not doc or doc.deleted_at or doc.pending_version_id != version_id:
            return False
        if current.status == 'queued' and current.attempts >= s.max_ingestion_attempts:
            current.status = 'failed'
            current.error = 'Maximum ingestion attempts reached'
            doc.status = 'ready' if doc.active_version_id else 'failed'
            return False
        result = db.execute(update(DocumentVersion).where(
            DocumentVersion.id == version_id, DocumentVersion.status == 'queued',
            DocumentVersion.attempts < s.max_ingestion_attempts,
        ).values(status='processing', attempts=DocumentVersion.attempts + 1,
                 worker_token=token, updated_at=utcnow(),
                 lease_expires_at=utcnow() + timedelta(seconds=s.worker_lease_seconds))).rowcount
        if result and not doc.active_version_id:
            doc.status = 'processing'
        return bool(result)


def _finish(version_id: str, token: str, error: str | None = None,
            pages: int = 0, chunks: int = 0, prefix: str | None = None,
            checksum: str | None = None, metadata_checksum: str | None = None):
    with SessionLocal.begin() as db:
        row = db.scalar(select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update())
        if not row or row.worker_token != token or row.status != 'processing':
            return False
        doc = db.scalar(select(DocumentRecord).where(DocumentRecord.id == row.document_id,
                                                     DocumentRecord.tenant_id == row.tenant_id).with_for_update())
        if not doc or doc.deleted_at or doc.pending_version_id != row.id:
            row.status = 'superseded'
            row.error = 'Document deleted or pending version replaced'
        elif error is None:
            row.status = 'ready'
            row.page_count = pages
            row.chunk_count = chunks
            row.index_prefix = prefix
            row.index_sha256 = checksum
            row.index_metadata_sha256 = metadata_checksum
            row.embedding_model = get_settings().openai_embedding_model
            row.published_at = utcnow()
            doc.active_version_id = row.id
            doc.pending_version_id = None
            doc.status = 'ready'
            doc.filename = row.filename
        else:
            row.status = 'failed'
            row.error = error[:500]
            doc.status = 'ready' if doc.active_version_id else 'failed'
        row.updated_at = utcnow()
        row.worker_token = None
        row.lease_expires_at = None
        return row.status == 'ready'


@celery_app.task(bind=True, name='app.tasks.ingest_version', max_retries=3)
def ingest_version(self, version_id: str):
    token = str(uuid.uuid4())
    if not _claim(version_id, token):
        return {'result': 'not_claimed', 'version_id': version_id}
    with SessionLocal() as db:
        version = db.get(DocumentVersion, version_id)
        tenant_id, document_id, source, digest, filename = (
            version.tenant_id, version.document_id, version.source_key, version.sha256, version.filename)
    prefix = index_prefix(tenant_id, document_id, version_id)
    try:
        create_dirs()
        with tempfile.TemporaryDirectory(dir=get_settings().data_dir / 'tmp') as temporary:
            base = Path(temporary)
            doc_path = base / ('source' + Path(filename).suffix.lower())
            get_store().get_file(source, doc_path)
            if hashlib.sha256(doc_path.read_bytes()).hexdigest() != digest:
                raise InvalidDocument('Stored document failed SHA256 integrity validation')
            scan_file(doc_path)
            pages = load_pages(doc_path)
            chunks = chunk_pages(pages)
            index_dir = base / 'index'
            checksum, metadata_checksum = write_index(chunks, index_dir)
            upload_index(index_dir, prefix)
        promoted = _finish(version_id, token, pages=len(pages), chunks=len(chunks),
                           prefix=prefix, checksum=checksum, metadata_checksum=metadata_checksum)
        if not promoted:
            get_store().delete_prefix(prefix)
        return {'result': 'processed' if promoted else 'superseded', 'version_id': version_id}
    except TRANSIENT as exc:
        logger.warning('Transient embedding failure version=%s', version_id, exc_info=True)
        if self.request.retries < self.max_retries:
            _finish_and_requeue(version_id, token)
            raise self.retry(exc=exc, countdown=min(180, 5 * 2 ** self.request.retries))
        _finish(version_id, token, error='Embedding provider unavailable after retries')
    except (InvalidDocument, SoftTimeLimitExceeded) as exc:
        _finish(version_id, token, error=str(exc))
    except Exception:
        logger.exception('Ingestion failure version=%s', version_id)
        _finish(version_id, token, error='Processing failed; check worker logs')
    return {'result': 'failed', 'version_id': version_id}


def _finish_and_requeue(version_id: str, token: str):
    with SessionLocal.begin() as db:
        row = db.scalar(select(DocumentVersion).where(DocumentVersion.id == version_id,
                                                       DocumentVersion.worker_token == token,
                                                       DocumentVersion.status == 'processing'))
        if row:
            row.status = 'queued'
            row.worker_token = None
            row.lease_expires_at = None
            row.last_dispatched_at = utcnow()


@celery_app.task(name='app.tasks.dispatch_pending')
def dispatch_pending():
    now = utcnow()
    with SessionLocal.begin() as db:
        recovered = db.execute(update(DocumentVersion).where(
            DocumentVersion.status == 'processing', DocumentVersion.lease_expires_at < now,
        ).values(status='queued', worker_token=None, lease_expires_at=None,
                 last_dispatched_at=None, updated_at=now)).rowcount
        # One beat process only. Duplicate broker messages remain safe to claim.
        queued = list(db.scalars(select(DocumentVersion.id).join(
            DocumentRecord, DocumentRecord.id == DocumentVersion.document_id).where(
                DocumentVersion.status == 'queued', DocumentRecord.deleted_at.is_(None),
                DocumentRecord.pending_version_id == DocumentVersion.id,
                or_(DocumentVersion.last_dispatched_at.is_(None),
                    DocumentVersion.last_dispatched_at < now - timedelta(seconds=90)),
            ).order_by(DocumentVersion.created_at).limit(300)))
        if queued:
            db.execute(update(DocumentVersion).where(DocumentVersion.id.in_(queued))
                       .values(last_dispatched_at=now))
    sent = 0
    for id_ in queued:
        try:
            ingest_version.delay(id_)
            sent += 1
        except Exception:
            logger.exception('Could not enqueue %s', id_)
            with SessionLocal.begin() as db:
                db.execute(update(DocumentVersion).where(DocumentVersion.id == id_,
                                                         DocumentVersion.status == 'queued')
                           .values(last_dispatched_at=None))
    return {'recovered': recovered, 'dispatched': sent}


@celery_app.task(name='app.tasks.garbage_collect')
def garbage_collect():
    now = utcnow()
    threshold = now - timedelta(minutes=60)
    purged = 0
    with SessionLocal() as db:
        deleted_documents = list(db.scalars(select(DocumentRecord).where(
            DocumentRecord.deleted_at.is_not(None), DocumentRecord.deleted_at < threshold).limit(100)))
    for doc in deleted_documents:
        with SessionLocal() as db:
            versions = list(db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc.id)))
        if any(v.lease_expires_at and v.lease_expires_at > now for v in versions):
            continue
        try:
            get_store().delete_prefix(document_prefix(doc.tenant_id, doc.id))
            for version in versions:
                shutil.rmtree(get_settings().data_dir / 'cache' / doc.tenant_id / version.id, ignore_errors=True)
        except Exception:
            logger.exception('Retention delete failed doc=%s', doc.id)
            continue
        with SessionLocal.begin() as db:
            db.execute(delete(DocumentVersion).where(DocumentVersion.document_id == doc.id))
            db.execute(delete(DocumentRecord).where(DocumentRecord.id == doc.id,
                                                    DocumentRecord.deleted_at.is_not(None)))
        purged += 1
    with SessionLocal.begin() as db:
        db.execute(delete(SignupIntent).where(SignupIntent.expires_at < now - timedelta(days=1)))
        db.execute(delete(Invitation).where(Invitation.expires_at < now - timedelta(days=1)))
        db.execute(delete(RefreshToken).where(RefreshToken.expires_at < now - timedelta(days=1)))
        db.execute(delete(AuditEvent).where(AuditEvent.created_at < now - timedelta(days=get_settings().audit_retention_days)))
        db.execute(delete(UsageEvent).where(UsageEvent.created_at < now - timedelta(days=get_settings().audit_retention_days)))
    return {'purged_documents': purged}
