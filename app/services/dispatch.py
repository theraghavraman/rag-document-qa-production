"""Best-effort queue dispatch, repaired by DB outbox sweep every 45 seconds."""
import logging
from app.database import SessionLocal
from app.models import DocumentVersion, utcnow

logger = logging.getLogger(__name__)


def publish_version(version_id: str) -> bool:
    from app.tasks import ingest_version
    try:
        ingest_version.delay(version_id)
    except Exception:
        logger.exception('Queue unavailable; outbox will retry version=%s', version_id)
        return False
    with SessionLocal() as db:
        row = db.get(DocumentVersion, version_id)
        if row and row.status == 'queued':
            row.last_dispatched_at = utcnow()
            db.commit()
    return True
