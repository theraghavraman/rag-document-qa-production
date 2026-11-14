"""Argon2 password hashing, revocable bearer tokens, and hashed API keys."""
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import APIKey, RefreshToken, Tenant, User, utcnow
from app.rate_limit import check_rate_limit

hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
ROLE_RANK = {'viewer': 0, 'member': 1, 'admin': 2, 'owner': 3}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    actor_id: str
    role: str
    user_id: str | None
    kind: str


def password_hash(value: str) -> str:
    if len(value) < 12 or len(value) > 128:
        raise ValueError('Passwords must be between 12 and 128 characters')
    return hasher.hash(value)


def verify_password(value: str, stored: str) -> bool:
    try:
        return hasher.verify(stored, value)
    except (VerificationError, ValueError):
        return False


def digest(value: str) -> str:
    return hmac.new(get_settings().api_key_pepper.encode(), value.encode(), hashlib.sha256).hexdigest()


def access_token(user: User) -> str:
    config = get_settings()
    now = utcnow()
    return jwt.encode(
        {'sub': user.id, 'tid': user.tenant_id, 'ver': user.token_version, 'type': 'access',
         'iss': config.jwt_issuer, 'aud': config.jwt_audience,
         'iat': now, 'nbf': now, 'exp': now + timedelta(minutes=config.access_token_minutes),
         'jti': secrets.token_hex(12)},
        config.jwt_secret, algorithm='HS256',
    )


def new_refresh(db: Session, user: User) -> str:
    token = 'rqr_' + secrets.token_urlsafe(48)
    db.add(RefreshToken(
        user_id=user.id, tenant_id=user.tenant_id, token_hash=digest(token),
        expires_at=utcnow() + timedelta(days=get_settings().refresh_token_days),
    ))
    return token


def create_api_key(db: Session, tenant_id: str, user_id: str, name: str, role: str = 'member') -> tuple[str, APIKey]:
    prefix = secrets.token_hex(7)
    value = f'rqa_{prefix}_{secrets.token_urlsafe(32)}'
    row = APIKey(tenant_id=tenant_id, created_by=user_id, prefix=prefix,
                 secret_hash=digest(value), name=name, role=role)
    db.add(row)
    db.flush()
    return value, row


def get_principal(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(None, alias='Authorization'),
    x_api_key: str | None = Header(None, alias='X-API-Key'),
) -> Principal:
    config = get_settings()
    bearer = authorization.split(' ', 1)[1] if authorization and authorization.startswith('Bearer ') else None
    if bool(bearer) == bool(x_api_key):
        raise HTTPException(401, 'Provide exactly one Bearer token or X-API-Key')

    if bearer:
        try:
            payload = jwt.decode(bearer, config.jwt_secret, algorithms=['HS256'],
                                 issuer=config.jwt_issuer, audience=config.jwt_audience,
                                 options={'require': ['exp', 'iat', 'sub', 'tid', 'ver', 'type']})
        except jwt.InvalidTokenError:
            raise HTTPException(401, 'Invalid or expired access token') from None
        if payload.get('type') != 'access':
            raise HTTPException(401, 'Invalid token type')
        user = db.get(User, payload['sub'])
        if not user or not user.is_active or user.tenant_id != payload['tid'] or user.token_version != payload['ver']:
            raise HTTPException(401, 'Account unavailable')
        principal = Principal(tenant_id=user.tenant_id, actor_id=user.id, role=user.role,
                              user_id=user.id, kind='user')
    else:
        # Never treat a user-supplied prefix or ID as a tenant authority.
        parts = (x_api_key or '').split('_', 2)
        if len(parts) != 3 or parts[0] != 'rqa' or len(parts[1]) != 14:
            raise HTTPException(401, 'Invalid API key')
        row = db.scalar(select(APIKey).where(APIKey.prefix == parts[1], APIKey.revoked_at.is_(None)))
        if not row or not hmac.compare_digest(digest(x_api_key), row.secret_hash):
            raise HTTPException(401, 'Invalid API key')
        principal = Principal(tenant_id=row.tenant_id, actor_id=row.id, role=row.role,
                              user_id=None, kind='api_key')
        # Bounded write frequency to avoid DB hot rows under sustained API load.
        if row.last_used_at is None or (utcnow() - as_utc(row.last_used_at)).total_seconds() > 300:
            row.last_used_at = utcnow()
            db.commit()
    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant or not tenant.is_active:
        raise HTTPException(403, 'Workspace unavailable')
    check_rate_limit(f'{principal.tenant_id}:{principal.actor_id}', config.rate_limit_per_minute)
    return principal


def as_utc(value):
    from datetime import timezone
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def require_role(minimum: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if ROLE_RANK.get(principal.role, -1) < ROLE_RANK[minimum]:
            raise HTTPException(403, 'Insufficient permissions')
        return principal
    return dependency
