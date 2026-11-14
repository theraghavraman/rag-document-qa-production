"""Tenant-scoped schema. Every externally addressable data row includes tenant_id."""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, BigInteger, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


def uid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = 'tenants'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(24), default='trial', nullable=False)
    billing_status: Mapped[str] = mapped_column(String(32), default='trialing', nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(100))
    last_billing_event_created: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    current_storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class User(Base):
    __tablename__ = 'users'
    __table_args__ = (UniqueConstraint('tenant_id', 'email', name='uq_user_email_tenant'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey('tenants.id'), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default='member', nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class APIKey(Base):
    __tablename__ = 'api_keys'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey('tenants.id'), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey('users.id'), nullable=False)
    prefix: Mapped[str] = mapped_column(String(18), unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default='member', nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RefreshToken(Base):
    __tablename__ = 'refresh_tokens'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey('tenants.id'), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey('users.id'), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DocumentRecord(Base):
    __tablename__ = 'documents'
    __table_args__ = (Index('ix_documents_tenant_status', 'tenant_id', 'status'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey('tenants.id'), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default='queued', nullable=False)
    active_version_id: Mapped[str | None] = mapped_column(String(36))
    pending_version_id: Mapped[str | None] = mapped_column(String(36))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DocumentVersion(Base):
    __tablename__ = 'document_versions'
    __table_args__ = (
        UniqueConstraint('document_id', 'version_number', name='uq_document_version'),
        Index('ix_version_work', 'status', 'last_dispatched_at'),
        Index('ix_version_tenant_document', 'tenant_id', 'document_id'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey('tenants.id'), nullable=False)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey('documents.id'), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_key: Mapped[str] = mapped_column(String(320), nullable=False)
    index_prefix: Mapped[str | None] = mapped_column(String(350))
    index_sha256: Mapped[str | None] = mapped_column(String(64))
    index_metadata_sha256: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(20), default='queued', nullable=False)
    worker_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsagePeriod(Base):
    __tablename__ = 'usage_periods'
    __table_args__ = (UniqueConstraint('tenant_id', 'period', name='uq_usage_period'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey('tenants.id'), index=True, nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    queries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uploads: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class UsageEvent(Base):
    __tablename__ = 'usage_events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey('tenants.id'), index=True, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey('tenants.id'), index=True, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(90), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(36))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BillingWebhookEvent(Base):
    __tablename__ = 'billing_webhook_events'
    id: Mapped[str] = mapped_column(String(110), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(110), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Invitation(Base):
    __tablename__ = 'invitations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey('tenants.id'), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SignupIntent(Base):
    __tablename__ = 'signup_intents'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    workspace_name: Mapped[str] = mapped_column(String(180), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
