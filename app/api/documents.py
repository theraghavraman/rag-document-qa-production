"""Tenant-isolated upload, versioning, recovery, and soft deletion."""
import hashlib
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import get_db
from app.models import DocumentRecord, DocumentVersion, Tenant, utcnow
from app.schemas import DocumentList, DocumentOut, VersionOut
from app.security import Principal, require_role
from app.services.dispatch import publish_version
from app.services.usage import audit, reserve_upload
from app.storage import (ALLOWED_EXTENSIONS, document_prefix, get_store, safe_filename,
                         scan_file, source_key)

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/v1/documents', tags=['documents'])


def _out(doc: DocumentRecord) -> DocumentOut:
    return DocumentOut(id=doc.id, filename=doc.filename, status=doc.status,
                       active_version_id=doc.active_version_id, pending_version_id=doc.pending_version_id,
                       created_at=doc.created_at, updated_at=doc.updated_at)


async def _temp_upload(file: UploadFile) -> tuple[Path, int, str, str]:
    filename = safe_filename(file.filename)
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, 'Supported formats: PDF, DOCX, TXT, MD')
    directory = get_settings().data_dir / 'tmp'
    directory.mkdir(parents=True, exist_ok=True)
    filename_temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, suffix=ext, delete=False) as stream:
            filename_temp = Path(stream.name)
            digest = hashlib.sha256()
            size, magic = 0, b''
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > get_settings().max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, 'File exceeds upload size limit')
                magic = (magic + chunk)[:5]
                stream.write(chunk)
                digest.update(chunk)
        if size == 0:
            raise HTTPException(422, 'Empty documents are not supported')
        if ext == '.pdf' and not magic.startswith(b'%PDF-'):
            raise HTTPException(422, 'Invalid PDF header')
        if ext == '.docx' and not magic.startswith(b'PK'):
            raise HTTPException(422, 'Invalid DOCX container')
        try:
            await run_in_threadpool(scan_file, filename_temp)
        except Exception:
            logger.warning('Document rejected by malware scanner', exc_info=True)
            raise HTTPException(422, 'File rejected or malware scanner unavailable') from None
        return filename_temp, size, digest.hexdigest(), filename
    except Exception:
        if filename_temp:
            filename_temp.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def _make_version(db: Session, tenant_id: str, actor_id: str, path: Path,
                  filename: str, size: int, sha256: str, doc_id: str | None = None) -> tuple[DocumentRecord, DocumentVersion]:
    # Serialize concurrent upload/replace against tenant quotas and version numbers.
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if not tenant:
        raise HTTPException(403, 'Workspace unavailable')
    doc = (db.scalar(select(DocumentRecord).where(DocumentRecord.id == doc_id,
                                                 DocumentRecord.tenant_id == tenant_id).with_for_update())
           if doc_id else None)
    if doc_id and (not doc or doc.deleted_at):
        raise HTTPException(404, 'Document not found')
    if doc and doc.pending_version_id:
        pending = db.get(DocumentVersion, doc.pending_version_id)
        if pending and pending.status in {'queued', 'processing'}:
            raise HTTPException(409, 'Document already has an ingestion in progress')
    current_count = (db.scalar(select(func.count()).select_from(DocumentVersion)
                               .where(DocumentVersion.document_id == doc.id)) if doc else 0) or 0
    latest_number = (db.scalar(select(func.max(DocumentVersion.version_number))
                               .where(DocumentVersion.document_id == doc.id)) if doc else 0) or 0
    if current_count >= get_settings().max_versions_per_document:
        raise HTTPException(409, 'Document revision limit reached; delete an old revision first')
    reserve_upload(db, tenant_id, size, is_new_document=doc is None, actor_id=actor_id)
    if doc is None:
        doc = DocumentRecord(tenant_id=tenant_id, filename=filename)
        db.add(doc)
        db.flush()
    version = DocumentVersion(tenant_id=tenant_id, document_id=doc.id,
                              version_number=latest_number + 1, filename=filename,
                              sha256=sha256, size_bytes=size, source_key='')
    db.add(version)
    db.flush()
    version.source_key = source_key(tenant_id, doc.id, version.id, Path(filename).suffix.lower())
    # Upload to durable private storage before committing the database pointer.
    try:
        get_store().put_file(path, version.source_key)
    except Exception:
        try:
            get_store().delete_prefix(version.source_key)
        except Exception:
            logger.exception('Could not clean up incomplete source upload')
        raise
    doc.pending_version_id = version.id
    if not doc.active_version_id:
        doc.status = 'queued'
    doc.updated_at = utcnow()
    audit(db, tenant_id, actor_id, 'document.version.uploaded', doc.id,
          {'version': version.version_number, 'bytes': size})
    return doc, version


async def _submit(file: UploadFile, principal: Principal, db: Session, doc_id: str | None = None):
    path, size, digest, filename = await _temp_upload(file)
    uploaded_key = None
    try:
        doc, version = await run_in_threadpool(_make_version, db, principal.tenant_id,
                                                principal.actor_id, path, filename, size, digest, doc_id)
        uploaded_key = version.source_key
        db.commit()
        result = _out(doc)
    except HTTPException:
        db.rollback()
        if uploaded_key:
            get_store().delete_prefix(uploaded_key)
        raise
    except Exception:
        db.rollback()
        if uploaded_key:
            get_store().delete_prefix(uploaded_key)
        logger.exception('Document upload failed')
        raise HTTPException(503, 'Unable to store document') from None
    finally:
        path.unlink(missing_ok=True)
    await run_in_threadpool(publish_version, version.id)
    return result


@router.post('', status_code=202, response_model=DocumentOut)
async def upload_document(file: Annotated[UploadFile, File()],
                          principal: Principal = Depends(require_role('member')),
                          db: Session = Depends(get_db)):
    return await _submit(file, principal, db)


@router.post('/{document_id}/versions', status_code=202, response_model=DocumentOut)
async def upload_revision(document_id: uuid.UUID, file: Annotated[UploadFile, File()],
                          principal: Principal = Depends(require_role('member')),
                          db: Session = Depends(get_db)):
    return await _submit(file, principal, db, str(document_id))


@router.get('', response_model=DocumentList)
def list_documents(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                   status: str | None = Query(None, description="Optional filter: queued, processing, ready, failed"),
                   principal: Principal = Depends(require_role('viewer')),
                   db: Session = Depends(get_db)):
    if status is not None and status not in {'queued', 'processing', 'ready', 'failed'}:
        raise HTTPException(422, 'Invalid status filter')
    where = [DocumentRecord.tenant_id == principal.tenant_id, DocumentRecord.deleted_at.is_(None)]
    if status is not None:
        where.append(DocumentRecord.status == status)
    count = db.scalar(select(func.count()).select_from(DocumentRecord).where(*where)) or 0
    docs = db.scalars(select(DocumentRecord).where(*where).order_by(DocumentRecord.created_at.desc())
                      .limit(limit).offset(offset)).all()
    return DocumentList(documents=[_out(doc) for doc in docs], total=count, limit=limit, offset=offset)


@router.get('/{document_id}', response_model=DocumentOut)
def get_document(document_id: uuid.UUID, principal: Principal = Depends(require_role('viewer')),
                 db: Session = Depends(get_db)):
    doc = db.scalar(select(DocumentRecord).where(DocumentRecord.id == str(document_id),
                                                 DocumentRecord.tenant_id == principal.tenant_id,
                                                 DocumentRecord.deleted_at.is_(None)))
    if not doc:
        raise HTTPException(404, 'Document not found')
    return _out(doc)


@router.get('/{document_id}/versions', response_model=list[VersionOut])
def versions(document_id: uuid.UUID, principal: Principal = Depends(require_role('viewer')),
             db: Session = Depends(get_db)):
    get_document(document_id, principal, db)
    return list(db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == str(document_id),
                                                        DocumentVersion.tenant_id == principal.tenant_id)
                           .order_by(DocumentVersion.version_number.desc())))


@router.post('/{document_id}/reindex', status_code=202, response_model=DocumentOut)
def reindex(document_id: uuid.UUID, principal: Principal = Depends(require_role('member')),
            db: Session = Depends(get_db)):
    doc = db.scalar(select(DocumentRecord).where(DocumentRecord.id == str(document_id),
                                                 DocumentRecord.tenant_id == principal.tenant_id,
                                                 DocumentRecord.deleted_at.is_(None)))
    if not doc:
        raise HTTPException(404, 'Document not found')
    pending = db.get(DocumentVersion, doc.pending_version_id) if doc.pending_version_id else None
    if pending and pending.status == 'failed':
        pending.status = 'queued'
        pending.attempts = 0
        pending.last_dispatched_at = None
        pending.error = None
        doc.status = 'ready' if doc.active_version_id else 'queued'
        db.commit()
        publish_version(pending.id)
        return _out(doc)
    if pending:
        raise HTTPException(409, 'Already processing')
    base = db.get(DocumentVersion, doc.active_version_id) if doc.active_version_id else None
    if not base:
        raise HTTPException(409, 'No indexed version available')
    # Reindex intentionally retains the source, but uses a new version and new object key.
    with tempfile.TemporaryDirectory(dir=get_settings().data_dir / 'tmp') as dirname:
        path = Path(dirname) / ('source' + Path(base.filename).suffix.lower())
        get_store().get_file(base.source_key, path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != base.sha256:
            raise HTTPException(409, 'Stored source checksum mismatch')
        new_doc, version = _make_version(db, principal.tenant_id, principal.actor_id, path,
                                          base.filename, base.size_bytes, base.sha256, doc.id)
        db.commit()
    publish_version(version.id)
    return _out(new_doc)


@router.delete('/{document_id}', status_code=204)
def delete_document(document_id: uuid.UUID, principal: Principal = Depends(require_role('member')),
                    db: Session = Depends(get_db)):
    tenant = db.scalar(select(Tenant).where(Tenant.id == principal.tenant_id).with_for_update())
    doc = db.scalar(select(DocumentRecord).where(DocumentRecord.id == str(document_id),
                                                 DocumentRecord.tenant_id == principal.tenant_id,
                                                 DocumentRecord.deleted_at.is_(None)).with_for_update())
    if not doc:
        raise HTTPException(404, 'Document not found')
    bytes_used = db.scalar(select(func.sum(DocumentVersion.size_bytes)).where(
        DocumentVersion.document_id == doc.id, DocumentVersion.tenant_id == principal.tenant_id)) or 0
    tenant.current_storage_bytes = max(0, tenant.current_storage_bytes - bytes_used)
    doc.deleted_at = utcnow()
    doc.status = 'deleted'
    audit(db, principal.tenant_id, principal.actor_id, 'document.deleted', doc.id)
    db.commit()
    return None


@router.delete('/{document_id}/versions/{version_id}', status_code=204)
def delete_old_revision(document_id: uuid.UUID, version_id: uuid.UUID,
                        principal: Principal = Depends(require_role('admin')),
                        db: Session = Depends(get_db)):
    tenant = db.scalar(select(Tenant).where(Tenant.id == principal.tenant_id).with_for_update())
    doc = db.scalar(select(DocumentRecord).where(DocumentRecord.id == str(document_id),
                                                 DocumentRecord.tenant_id == principal.tenant_id,
                                                 DocumentRecord.deleted_at.is_(None)).with_for_update())
    if not doc:
        raise HTTPException(404, 'Document not found')
    version = db.scalar(select(DocumentVersion).where(DocumentVersion.id == str(version_id),
                                                     DocumentVersion.document_id == doc.id,
                                                     DocumentVersion.tenant_id == principal.tenant_id).with_for_update())
    if not version:
        raise HTTPException(404, 'Version not found')
    if doc.active_version_id == version.id or doc.pending_version_id == version.id or version.status == 'processing':
        raise HTTPException(409, 'Cannot remove an active or processing revision')
    prefix = source_key(principal.tenant_id, doc.id, version.id,
                        Path(version.filename).suffix.lower()).rsplit('/', 1)[0] + '/'
    try:
        get_store().delete_prefix(prefix)
    except Exception:
        logger.exception('Version object purge failed')
        raise HTTPException(503, 'Object-store deletion failed') from None
    tenant.current_storage_bytes = max(0, tenant.current_storage_bytes - version.size_bytes)
    db.delete(version)
    audit(db, principal.tenant_id, principal.actor_id, 'document.version.deleted', doc.id,
          {'version_id': str(version_id)})
    db.commit()
    return None
