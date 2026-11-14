"""Workspace usage, membership controls and tenant-scoped audit trail."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import APIKey, AuditEvent, DocumentRecord, Tenant, UsagePeriod, User, utcnow
from app.security import Principal, require_role
from app.services.usage import PLAN_LIMITS, audit, limits

router = APIRouter(prefix='/v1/workspace', tags=['workspace'])


@router.get('/usage')
def usage(principal: Principal = Depends(require_role('viewer')), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, principal.tenant_id)
    period = utcnow().strftime('%Y-%m')
    row = db.scalar(select(UsagePeriod).where(UsagePeriod.tenant_id == tenant.id,
                                               UsagePeriod.period == period))
    count = db.scalar(select(func.count()).select_from(DocumentRecord).where(
        DocumentRecord.tenant_id == tenant.id, DocumentRecord.deleted_at.is_(None))) or 0
    return {'plan': tenant.plan, 'billing_status': tenant.billing_status,
            'period': period,
            'usage': {'queries': row.queries if row else 0, 'uploads': row.uploads if row else 0,
                      'documents': count, 'storage_bytes': tenant.current_storage_bytes,
                      'input_tokens': row.input_tokens if row else 0,
                      'output_tokens': row.output_tokens if row else 0},
            'limits': limits(tenant)}


@router.get('/users')
def users(principal: Principal = Depends(require_role('admin')), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.tenant_id == principal.tenant_id)
                      .order_by(User.created_at)).all()
    return [{'id': row.id, 'email': row.email, 'role': row.role, 'is_active': row.is_active,
             'created_at': row.created_at} for row in rows]


class UpdateUser(BaseModel):
    role: str | None = None
    is_active: bool | None = None


@router.patch('/users/{user_id}')
def update_user(user_id: str, data: UpdateUser,
                principal: Principal = Depends(require_role('owner')), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.id == user_id,
                                        User.tenant_id == principal.tenant_id).with_for_update())
    if not user:
        raise HTTPException(404, 'User not found')
    if data.role is not None and data.role not in {'viewer', 'member', 'admin', 'owner'}:
        raise HTTPException(422, 'Invalid role')
    if user.id == principal.user_id and (data.is_active is False or (data.role and data.role != 'owner')):
        raise HTTPException(409, 'Cannot remove own owner access')
    if data.role and user.role == 'owner' and data.role != 'owner':
        owners = db.scalar(select(func.count()).select_from(User).where(
            User.tenant_id == user.tenant_id, User.role == 'owner', User.is_active.is_(True)))
        if owners <= 1:
            raise HTTPException(409, 'Workspace requires at least one active owner')
    if data.is_active is False and user.role == 'owner':
        owners = db.scalar(select(func.count()).select_from(User).where(
            User.tenant_id == user.tenant_id, User.role == 'owner', User.is_active.is_(True)))
        if owners <= 1:
            raise HTTPException(409, 'Workspace requires at least one active owner')
    if data.role is not None:
        user.role = data.role
    if data.is_active is not None:
        user.is_active = data.is_active
    user.token_version += 1
    if not user.is_active:
        db.execute(update(APIKey).where(APIKey.created_by == user.id,
                                        APIKey.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, principal.tenant_id, principal.actor_id, 'user.updated', user.id)
    db.commit()
    return {'id': user.id, 'role': user.role, 'is_active': user.is_active}


@router.get('/audit')
def audits(limit: int = Query(50, ge=1, le=100),
           principal: Principal = Depends(require_role('admin')), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == principal.tenant_id)
                      .order_by(AuditEvent.created_at.desc()).limit(limit)).all()
    return [{'id': row.id, 'actor_id': row.actor_id, 'action': row.action,
             'resource_id': row.resource_id, 'created_at': row.created_at,
             'metadata': row.metadata_json} for row in rows]
