"""Workspace sign-in, token rotation, invitations, and credential management."""
import secrets
from ipaddress import ip_address, ip_network
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import APIKey, Invitation, RefreshToken, SignupIntent, Tenant, User, utcnow
from app.rate_limit import check_rate_limit
from app.schemas import (AcceptInvitationIn, ApiKeyIn, APIKeyOut, ChangePasswordIn, InvitationIn,
                         LoginIn, MeOut, RefreshIn, RegistrationIn, TokenOut, TokenVerificationIn)
from app.security import (Principal, access_token, as_utc, create_api_key, digest, get_principal,
                          new_refresh, password_hash, require_role, verify_password)
from app.services.usage import audit
from app.services.mailer import send_access_link

router = APIRouter(prefix='/v1/auth', tags=['authentication'])


def _tokens(db: Session, user: User) -> TokenOut:
    access = access_token(user)
    refresh = new_refresh(db, user)
    db.commit()
    return TokenOut(access_token=access, refresh_token=refresh,
                    expires_in=get_settings().access_token_minutes * 60)


def _limit_public(request: Request):
    # For production put API behind ingress that forwards true client IP securely.
    ip = request.client.host if request.client else 'unknown'
    trusted = [ip_network(item.strip(), strict=False) for item in
               get_settings().trusted_proxy_cidrs.split(',') if item.strip()]
    try:
        peer_trusted = any(ip_address(ip) in network for network in trusted)
    except ValueError:
        peer_trusted = False
    if peer_trusted:
        # Walk XFF from right to left; the trusted ingress must append real client IP.
        # Never trust client-supplied XFF if TCP peer is not in TRUSTED_PROXY_CIDRS.
        values = [x.strip() for x in request.headers.get('X-Forwarded-For', '').split(',') if x.strip()]
        for candidate in reversed(values):
            try:
                if not any(ip_address(candidate) in n for n in trusted):
                    ip = candidate
                    break
            except ValueError:
                continue
    check_rate_limit('login:' + ip, 15)


@router.post('/register', response_model=TokenOut, status_code=201)
def register(data: RegistrationIn, request: Request, db: Session = Depends(get_db)):
    _limit_public(request)
    if not get_settings().enable_public_signup:
        raise HTTPException(403, 'Public signup disabled; request a workspace invitation')
    if get_settings().app_env == 'production':
        raise HTTPException(403, 'Use /v1/auth/register/start and verify your email')
    tenant = Tenant(name=data.workspace_name, slug=data.workspace_slug)
    db.add(tenant)
    try:
        db.flush()
        user = User(tenant_id=tenant.id, email=data.email.lower(),
                    password_hash=password_hash(data.password), role='owner')
        db.add(user)
        db.flush()
        audit(db, tenant.id, user.id, 'tenant.created', tenant.id)
        return _tokens(db, user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Workspace slug already exists') from None


@router.post('/login', response_model=TokenOut)
def login(data: LoginIn, request: Request, db: Session = Depends(get_db)):
    _limit_public(request)
    tenant = db.scalar(select(Tenant).where(Tenant.slug == data.workspace_slug))
    user = (db.scalar(select(User).where(User.tenant_id == tenant.id, User.email == data.email.lower()))
            if tenant and tenant.is_active else None)
    if not user or not user.is_active or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, 'Invalid credentials')
    return _tokens(db, user)


@router.post('/refresh', response_model=TokenOut)
def refresh(data: RefreshIn, request: Request, db: Session = Depends(get_db)):
    _limit_public(request)
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == digest(data.refresh_token))
                    .with_for_update())
    if not row or row.revoked_at or as_utc(row.expires_at) < utcnow():
        raise HTTPException(401, 'Invalid refresh token')
    user = db.get(User, row.user_id)
    tenant = db.get(Tenant, row.tenant_id)
    if not user or not user.is_active or not tenant or not tenant.is_active:
        raise HTTPException(401, 'Account unavailable')
    row.revoked_at = utcnow()
    return _tokens(db, user)


@router.post('/logout', status_code=204)
def logout(data: RefreshIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == digest(data.refresh_token),
                                             RefreshToken.tenant_id == principal.tenant_id))
    if row and row.user_id == principal.user_id:
        row.revoked_at = utcnow()
        db.commit()


@router.get('/me', response_model=MeOut)
def me(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    if not principal.user_id:
        raise HTTPException(403, 'Dashboard requires a user account')
    tenant = db.get(Tenant, principal.tenant_id)
    user = db.get(User, principal.user_id)
    return MeOut(id=user.id, tenant_id=tenant.id, email=user.email, role=user.role,
                 workspace_name=tenant.name, workspace_slug=tenant.slug,
                 plan=tenant.plan, billing_status=tenant.billing_status)


@router.post('/password', status_code=204)
def change_password(data: ChangePasswordIn, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    if not principal.user_id:
        raise HTTPException(403, 'User account required')
    user = db.get(User, principal.user_id)
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(401, 'Incorrect current password')
    user.password_hash = password_hash(data.new_password)
    user.token_version += 1
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id,
                                          RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, user.tenant_id, user.id, 'user.password_changed', user.id)
    db.commit()


@router.post('/keys', status_code=201)
def issue_key(data: ApiKeyIn, principal: Principal = Depends(require_role('admin')),
              db: Session = Depends(get_db)):
    if not principal.user_id:
        raise HTTPException(403, 'Human administrator required')
    secret, row = create_api_key(db, principal.tenant_id, principal.user_id, data.name, data.role)
    audit(db, principal.tenant_id, principal.actor_id, 'api_key.created', row.id)
    db.commit()
    return {'api_key': secret, 'id': row.id, 'prefix': row.prefix,
            'message': 'Copy now. It will never be shown again.'}


@router.get('/keys', response_model=list[APIKeyOut])
def list_keys(principal: Principal = Depends(require_role('admin')), db: Session = Depends(get_db)):
    return list(db.scalars(select(APIKey).where(APIKey.tenant_id == principal.tenant_id)
                           .order_by(APIKey.created_at.desc()).limit(100)))


@router.delete('/keys/{key_id}', status_code=204)
def revoke_key(key_id: str, principal: Principal = Depends(require_role('admin')),
               db: Session = Depends(get_db)):
    row = db.scalar(select(APIKey).where(APIKey.id == key_id, APIKey.tenant_id == principal.tenant_id))
    if not row:
        raise HTTPException(404, 'API key not found')
    row.revoked_at = utcnow()
    audit(db, principal.tenant_id, principal.actor_id, 'api_key.revoked', row.id)
    db.commit()


@router.post('/invitations', status_code=201)
def invite(data: InvitationIn, principal: Principal = Depends(require_role('admin')),
           db: Session = Depends(get_db)):
    if not principal.user_id:
        raise HTTPException(403, 'Human administrator required')
    if db.scalar(select(User).where(User.tenant_id == principal.tenant_id,
                                    User.email == data.email.lower())):
        raise HTTPException(409, 'User already exists')
    token = 'rqi_' + secrets.token_urlsafe(40)
    row = Invitation(tenant_id=principal.tenant_id, email=data.email.lower(), role=data.role,
                     token_hash=digest(token), expires_at=utcnow() + timedelta(hours=48))
    db.add(row)
    audit(db, principal.tenant_id, principal.actor_id, 'invitation.created', metadata={'role': data.role})
    db.commit()
    if get_settings().smtp_enabled:
        try:
            send_access_link(row.email, token, 'invite')
        except Exception:
            # Admin can create a replacement invite if email delivery fails.
            raise HTTPException(503, 'Invitation saved but email delivery failed; create a new invitation') from None
        return {'delivery': 'email', 'expires_in_hours': 48}
    return {'invitation_token': token, 'expires_in_hours': 48, 'delivery': 'manual'}


@router.post('/invitations/accept', response_model=TokenOut)
def accept_invite(data: AcceptInvitationIn, request: Request, db: Session = Depends(get_db)):
    _limit_public(request)
    row = db.scalar(select(Invitation).where(Invitation.token_hash == digest(data.token)).with_for_update())
    if not row or row.used_at or as_utc(row.expires_at) < utcnow():
        raise HTTPException(400, 'Invitation invalid or expired')
    tenant = db.get(Tenant, row.tenant_id)
    if not tenant or not tenant.is_active:
        raise HTTPException(403, 'Workspace unavailable')
    if db.scalar(select(User).where(User.tenant_id == row.tenant_id, User.email == row.email)):
        raise HTTPException(409, 'Account already exists')
    user = User(tenant_id=row.tenant_id, email=row.email, role=row.role,
                password_hash=password_hash(data.password))
    db.add(user)
    db.flush()
    row.used_at = utcnow()
    audit(db, row.tenant_id, user.id, 'invitation.accepted', user.id)
    return _tokens(db, user)


@router.post('/register/start', status_code=202)
def registration_start(data: RegistrationIn, request: Request, db: Session = Depends(get_db)):
    _limit_public(request)
    if not get_settings().enable_public_signup or not get_settings().smtp_enabled:
        raise HTTPException(403, 'Email-verified signup is unavailable')
    if db.scalar(select(Tenant).where(Tenant.slug == data.workspace_slug)):
        raise HTTPException(409, 'Workspace slug unavailable')
    pending = db.scalar(select(SignupIntent).where(SignupIntent.slug == data.workspace_slug,
                                                   SignupIntent.used_at.is_(None)).with_for_update())
    if pending and as_utc(pending.expires_at) > utcnow():
        raise HTTPException(409, 'Workspace registration already pending')
    if pending:
        db.delete(pending)
        db.flush()
    token = 'rqv_' + secrets.token_urlsafe(42)
    record = SignupIntent(slug=data.workspace_slug, workspace_name=data.workspace_name,
                          email=data.email.lower(), password_hash=password_hash(data.password),
                          token_hash=digest(token), expires_at=utcnow() + timedelta(hours=48))
    db.add(record)
    db.commit()
    try:
        send_access_link(record.email, token, 'verify')
    except Exception:
        # An undelivered token must not block the same slug for 48h.
        db.delete(record)
        db.commit()
        raise HTTPException(503, 'Could not send verification email; retry later') from None
    return {'message': 'A verification link has been sent if the email can receive mail.'}


@router.post('/register/verify', response_model=TokenOut)
def registration_verify(data: TokenVerificationIn, request: Request, db: Session = Depends(get_db)):
    _limit_public(request)
    pending = db.scalar(select(SignupIntent).where(SignupIntent.token_hash == digest(data.token)).with_for_update())
    if not pending or pending.used_at or as_utc(pending.expires_at) < utcnow():
        raise HTTPException(400, 'Verification token invalid or expired')
    if db.scalar(select(Tenant).where(Tenant.slug == pending.slug)):
        raise HTTPException(409, 'Workspace slug unavailable')
    tenant = Tenant(slug=pending.slug, name=pending.workspace_name)
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, email=pending.email, password_hash=pending.password_hash, role='owner')
    db.add(user)
    db.flush()
    pending.used_at = utcnow()
    audit(db, tenant.id, user.id, 'tenant.verified_signup', tenant.id)
    return _tokens(db, user)
