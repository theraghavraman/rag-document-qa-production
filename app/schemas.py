"""Public API contracts: no secrets or raw storage paths in responses."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegistrationIn(BaseModel):
    workspace_name: str = Field(min_length=2, max_length=180)
    workspace_slug: str = Field(pattern=r'^[a-z][a-z0-9-]{2,59}$')
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class LoginIn(BaseModel):
    workspace_slug: str
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=12, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = 'bearer'
    expires_in: int


class MeOut(BaseModel):
    id: str
    tenant_id: str
    email: str
    role: str
    workspace_name: str
    workspace_slug: str
    plan: str
    billing_status: str


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    role: Literal['viewer', 'member', 'admin'] = 'member'


class APIKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    prefix: str
    role: str
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class InvitationIn(BaseModel):
    email: EmailStr
    role: Literal['viewer', 'member', 'admin'] = 'member'


class AcceptInvitationIn(BaseModel):
    token: str
    password: str = Field(min_length=12, max_length=128)


class DocumentOut(BaseModel):
    id: str
    filename: str
    status: str
    active_version_id: str | None
    pending_version_id: str | None
    created_at: datetime
    updated_at: datetime
    version_count: int | None = None


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version_number: int
    status: str
    filename: str
    size_bytes: int
    sha256: str
    page_count: int
    chunk_count: int
    attempts: int
    error: str | None
    created_at: datetime
    published_at: datetime | None


class DocumentList(BaseModel):
    documents: list[DocumentOut]
    total: int
    limit: int
    offset: int


class QueryIn(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    document_ids: list[str] | None = Field(None, min_length=1, max_length=50)
    top_k: int = Field(5, ge=1, le=10)
    min_similarity: float | None = Field(None, ge=-1, le=1)


class SourceOut(BaseModel):
    source_id: str
    document_id: str
    version_id: str
    filename: str
    page: int | None
    chunk_index: int
    similarity: float
    rank_score: float
    snippet: str


class QueryOut(BaseModel):
    answer: str
    grounded: bool
    citations_valid: bool = False
    sources: list[SourceOut]
    model: str | None
    input_tokens: int = 0
    output_tokens: int = 0


class TokenVerificationIn(BaseModel):
    token: str
