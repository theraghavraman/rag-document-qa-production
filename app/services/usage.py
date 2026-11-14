"""Transactionally enforce tenant quotas under a PostgreSQL row lock."""
from datetime import timezone
from fastapi import HTTPException
from sqlalchemy import func, select, delete
from sqlalchemy.orm import Session

from app.models import DocumentRecord, Tenant, UsagePeriod, UsageEvent, AuditEvent, utcnow

PLAN_LIMITS = {
    'trial': {'queries': 100, 'uploads': 30, 'documents': 20, 'bytes': 100 * 1024 ** 2},
    'starter': {'queries': 5000, 'uploads': 1000, 'documents': 500, 'bytes': 5 * 1024 ** 3},
    'business': {'queries': 50000, 'uploads': 10000, 'documents': 5000, 'bytes': 100 * 1024 ** 3},
    'enterprise': {'queries': 1000000, 'uploads': 100000, 'documents': 100000, 'bytes': 2 * 1024 ** 4},
}


def _period() -> str:
    return utcnow().strftime('%Y-%m')


def _lock_tenant(db: Session, tenant_id: str) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if not tenant or not tenant.is_active:
        raise HTTPException(403, 'Workspace unavailable')
    if tenant.billing_status in {'suspended', 'unpaid', 'incomplete_expired'}:
        raise HTTPException(402, 'Subscription needs attention')
    return tenant


def _usage(db: Session, tenant_id: str) -> UsagePeriod:
    row = db.scalar(select(UsagePeriod).where(UsagePeriod.tenant_id == tenant_id,
                                               UsagePeriod.period == _period()))
    if row is None:
        row = UsagePeriod(tenant_id=tenant_id, period=_period())
        db.add(row)
        db.flush()
    return row


def limits(tenant: Tenant) -> dict:
    return PLAN_LIMITS.get(tenant.plan, PLAN_LIMITS['trial'])


def reserve_upload(db: Session, tenant_id: str, file_bytes: int, is_new_document: bool, actor_id: str):
    tenant = _lock_tenant(db, tenant_id)
    maximum = limits(tenant)
    usage = _usage(db, tenant_id)
    if usage.uploads >= maximum['uploads']:
        raise HTTPException(402, 'Monthly upload quota exceeded')
    if tenant.current_storage_bytes + file_bytes > maximum['bytes']:
        raise HTTPException(402, 'Workspace storage quota exceeded')
    if is_new_document:
        count = db.scalar(select(func.count()).select_from(DocumentRecord).where(
            DocumentRecord.tenant_id == tenant_id, DocumentRecord.deleted_at.is_(None))) or 0
        if count >= maximum['documents']:
            raise HTTPException(402, 'Document count limit reached')
    usage.uploads += 1
    tenant.current_storage_bytes += file_bytes
    db.add(UsageEvent(tenant_id=tenant_id, actor_id=actor_id, type='upload'))


def reserve_query(db: Session, tenant_id: str, actor_id: str):
    tenant = _lock_tenant(db, tenant_id)
    usage = _usage(db, tenant_id)
    if usage.queries >= limits(tenant)['queries']:
        raise HTTPException(402, 'Monthly question quota exceeded')
    usage.queries += 1
    event = UsageEvent(tenant_id=tenant_id, actor_id=actor_id, type='query')
    db.add(event)
    db.flush()
    return event.id


def refund_failed_query(db: Session, tenant_id: str, event_id: str):
    # Compensate provider and index failures so failed questions are not counted.
    # Refund reservations even if billing status changed during the LLM request.
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    event = db.scalar(select(UsageEvent).where(UsageEvent.id == event_id,
                                              UsageEvent.tenant_id == tenant_id,
                                              UsageEvent.type == 'query').with_for_update())
    if event:
        usage = _usage(db, tenant_id)
        usage.queries = max(0, usage.queries - 1)
        db.delete(event)


def record_tokens(db: Session, tenant_id: str, input_tokens: int, output_tokens: int):
    # The query was already authorized/reserved; a mid-flight plan change must not discard accounting.
    db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    usage = _usage(db, tenant_id)
    usage.input_tokens += max(0, input_tokens)
    usage.output_tokens += max(0, output_tokens)


def audit(db: Session, tenant_id: str, actor_id: str, action: str,
          resource_id: str | None = None, metadata: dict | None = None):
    db.add(AuditEvent(tenant_id=tenant_id, actor_id=actor_id, action=action,
                      resource_id=resource_id, metadata_json=metadata or {}))
