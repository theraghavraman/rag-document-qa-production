"""Validated configuration: production refuses insecure/local defaults."""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    app_env: Literal['test', 'development', 'production'] = 'development'
    app_name: str = 'RAG Platform'
    database_url: str = 'sqlite:///./data/rag.db'
    redis_url: str = 'redis://localhost:6379/0'
    jwt_secret: str = 'local-only-replace-before-deployment'
    api_key_pepper: str = 'local-only-replace-before-deployment'
    jwt_issuer: str = 'rag-platform'
    jwt_audience: str = 'rag-platform-api'
    access_token_minutes: int = Field(15, ge=3, le=120)
    refresh_token_days: int = Field(14, ge=1, le=60)
    cors_origins: str = 'http://localhost:8000'
    trusted_proxy_cidrs: str = ''
    enable_public_signup: bool = False
    auto_create_tables: bool = False
    metrics_token: str = ''
    sentry_dsn: str = ''
    log_level: str = 'INFO'

    storage_backend: Literal['local', 's3'] = 'local'
    data_dir: Path = Path('./data')
    s3_bucket: str = ''
    s3_endpoint_url: str | None = None
    s3_region: str = 'us-east-1'
    s3_access_key_id: str = ''
    s3_secret_access_key: str = ''
    s3_secure: bool = True

    clamav_enabled: bool = False
    clamav_host: str = 'clamav'
    clamav_port: int = 3310
    max_upload_mb: int = Field(20, ge=1, le=200)
    max_request_body_mb: int = Field(22, ge=1, le=220)
    max_pdf_pages: int = Field(250, ge=1, le=2000)
    max_document_chars: int = Field(1_000_000, ge=100)
    max_chunks: int = Field(2500, ge=1)
    chunk_size: int = Field(900, ge=150)
    chunk_overlap: int = Field(120, ge=0)
    embedding_batch_size: int = Field(64, ge=1, le=256)
    enable_ocr: bool = True
    max_ocr_pages: int = Field(100, ge=1)

    llm_provider: Literal['openai', 'anthropic'] = 'openai'
    openai_api_key: str = ''
    openai_base_url: str | None = None
    openai_embedding_model: str = 'text-embedding-3-small'
    openai_chat_model: str = 'gpt-4.1-mini'
    anthropic_api_key: str = ''
    anthropic_chat_model: str = 'claude-sonnet-4-5'
    retrieval_mode: Literal['hybrid', 'dense'] = 'hybrid'
    reranker: Literal['none', 'cross-encoder'] = 'none'
    reranker_model: str = 'cross-encoder/ms-marco-MiniLM-L-6-v2'
    max_query_documents: int = Field(50, ge=1, le=250)
    max_context_chars: int = Field(12000, ge=500)
    min_similarity: float = Field(.22, ge=-1, le=1)
    query_timeout_seconds: int = Field(60, ge=5, le=180)

    worker_lease_seconds: int = Field(2700, ge=1500)
    max_ingestion_attempts: int = Field(5, ge=1, le=20)
    audit_retention_days: int = Field(90, ge=30)
    max_versions_per_document: int = Field(10, ge=1, le=100)
    rate_limit_per_minute: int = Field(60, ge=1, le=100000)

    smtp_enabled: bool = False
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_user: str = ''
    smtp_password: str = ''
    smtp_sender: str = ''
    stripe_enabled: bool = False
    stripe_secret_key: str = ''
    stripe_webhook_secret: str = ''
    stripe_starter_price_id: str = ''
    stripe_business_price_id: str = ''
    public_app_url: str = 'http://localhost:8000'

    @model_validator(mode='after')
    def validate_settings(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError('CHUNK_OVERLAP must be smaller than CHUNK_SIZE')
        if self.app_env == 'production':
            def placeholder(value):
                lower = value.lower()
                return not value or any(x in lower for x in ('replace', 'change-me', 'local-only', 'example', 'rotate'))
            if placeholder(self.database_url) or placeholder(self.redis_url):
                raise ValueError('Production connection URLs contain missing/default credentials')
            if any(not origin.strip().startswith('https://') or '*' in origin
                   for origin in self.cors_origins.split(',')):
                raise ValueError('Production CORS must list explicit HTTPS origins')
            if not self.trusted_proxy_cidrs:
                raise ValueError('Configure known ingress proxy CIDRs for accurate anti-abuse rate limits')
            if self.jwt_secret == self.api_key_pepper:
                raise ValueError('JWT and API key secrets must be different')
            if len(self.jwt_secret) < 32 or placeholder(self.jwt_secret):
                raise ValueError('JWT_SECRET must be a unique random secret (at least 32 chars)')
            if len(self.api_key_pepper) < 32 or placeholder(self.api_key_pepper):
                raise ValueError('API_KEY_PEPPER must be a unique random secret (at least 32 chars)')
            if self.storage_backend != 's3' or not self.s3_bucket:
                raise ValueError('Production requires private S3-compatible object storage')
            if not self.clamav_enabled:
                raise ValueError('Production requires document malware scanning')
            if self.s3_endpoint_url and not self.s3_endpoint_url.startswith('https://'):
                raise ValueError('Production S3-compatible endpoint must use HTTPS')
            if not self.database_url.startswith('postgresql+'):
                raise ValueError('Production requires PostgreSQL')
            if not self.redis_url.startswith('rediss://'):
                raise ValueError('Production requires TLS Redis (rediss://)')
            if not self.public_app_url.startswith('https://'):
                raise ValueError('Production public app URL must use HTTPS')
            if len(self.metrics_token) < 24 or placeholder(self.metrics_token):
                raise ValueError('Production requires METRICS_TOKEN')
            # Embeddings always use OpenAI, even when Anthropic generates answers.
            if placeholder(self.openai_api_key):
                raise ValueError('OPENAI_API_KEY is required for document/query embeddings')
            if self.llm_provider == 'anthropic' and placeholder(self.anthropic_api_key):
                raise ValueError('Anthropic chat provider credentials missing')
            if self.smtp_enabled and not self.smtp_starttls:
                raise ValueError('Production SMTP must enforce STARTTLS')
            if self.enable_public_signup and not self.smtp_enabled:
                raise ValueError('Production public signup requires SMTP email verification')
            if self.auto_create_tables:
                raise ValueError('Use Alembic migrations in production')
        if self.storage_backend == 's3' and not self.s3_bucket:
            raise ValueError('S3_BUCKET is required for S3 backend')
        if self.smtp_enabled and not all((self.smtp_host, self.smtp_sender)):
            raise ValueError('SMTP host and sender are required when mail is enabled')
        if self.stripe_enabled and not all((self.stripe_secret_key, self.stripe_webhook_secret)):
            raise ValueError('Stripe secret key and webhook secret required when enabled')
        if self.app_env == 'production' and self.stripe_enabled and any(
            placeholder(x) for x in (self.stripe_secret_key, self.stripe_webhook_secret,
                                     self.stripe_starter_price_id, self.stripe_business_price_id)):
            raise ValueError('Stripe production credentials and price IDs are not configured')
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
