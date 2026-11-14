"""ASGI application, security middleware, health, Prometheus metrics and console."""
import json
import logging
import re
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, Gauge, generate_latest
from redis import Redis
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sqlalchemy import text, select, func
from starlette.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.billing import router as billing_router
from app.api.documents import router as documents_router
from app.api.qa import router as qa_router
from app.config import get_settings
from app.database import Base, engine
from app.storage import create_dirs, get_store
from app.models import DocumentVersion

s = get_settings()
logging.basicConfig(level=s.log_level.upper(), format='%(message)s')
_LOG_SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/-]{12,}|\bsk-[A-Za-z0-9_-]{16,}\b|\bAKIA[A-Z0-9]{16}\b|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b)"
)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _LOG_SECRET_RE.sub("[REDACTED]", record.msg)
        if record.args:
            record.args = tuple(
                _LOG_SECRET_RE.sub("[REDACTED]", a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True


logger = logging.getLogger('rag')
logger.addFilter(SecretRedactionFilter())
requests_total = Counter('rag_http_requests_total', 'HTTP requests', ['method', 'route', 'status'])
requests_time = Histogram('rag_http_request_seconds', 'HTTP request duration', ['method', 'route'])
version_states = Gauge('rag_ingestion_versions', 'Versions in each database state', ['status'])

if s.sentry_dsn:
    import sentry_sdk
    sentry_sdk.init(dsn=s.sentry_dsn, integrations=[FastApiIntegration()],
                    traces_sample_rate=0.1, send_default_pii=False)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_dirs()
    if s.auto_create_tables:
        if s.app_env == 'production':
            raise RuntimeError('Production schema must be migrated with Alembic')
        from app import models  # noqa: F401
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title='RAG Document Q&A Platform', version='2.0.0', lifespan=lifespan)
if s.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in s.cors_origins.split(',') if x.strip()],
                       allow_credentials=False, allow_methods=['GET', 'POST', 'DELETE', 'PATCH'],
                       allow_headers=['Authorization', 'X-API-Key', 'Content-Type', 'X-Request-ID'])


@app.middleware('http')
async def observability(request: Request, call_next):
    started = time.monotonic()
    request_id = request.headers.get('X-Request-ID', '')
    if len(request_id) > 64 or not request_id.isascii() or not request_id.isprintable():
        request_id = ''
    request_id = request_id or str(uuid.uuid4())
    request.state.request_id = request_id
    cl = request.headers.get('content-length')
    if cl:
        try:
            if int(cl) > s.max_request_body_mb * 1024 * 1024:
                return Response(
                    content=json.dumps({'detail': f'Request body exceeds {s.max_request_body_mb} MiB ingress limit'}),
                    status_code=413,
                    media_type='application/json',
                    headers={'X-Request-ID': request_id, 'X-Content-Type-Options': 'nosniff'},
                )
        except ValueError:
            return Response(
                content=json.dumps({'detail': 'Invalid Content-Length header'}),
                status_code=400,
                media_type='application/json',
                headers={'X-Request-ID': request_id, 'X-Content-Type-Options': 'nosniff'},
            )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception('Unhandled request failure id=%s', request_id)
        raise
    route = request.scope.get('route')
    route_path = getattr(route, 'path', '/unknown')
    elapsed = time.monotonic() - started
    requests_total.labels(request.method, route_path, str(response.status_code)).inc()
    requests_time.labels(request.method, route_path).observe(elapsed)
    response.headers['X-Request-ID'] = request_id
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/v1/') else 'no-cache'
    if s.app_env == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    logger.info(json.dumps({'level': 'INFO', 'request_id': request_id,
                            'method': request.method, 'route': route_path, 'status': response.status_code,
                            'duration_ms': round(elapsed * 1000, 2)}))
    return response


app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(qa_router)
app.include_router(admin_router)
app.include_router(billing_router)

web_dir = Path(__file__).parent / 'web'
app.mount('/assets', StaticFiles(directory=str(web_dir)), name='assets')


@app.get('/app', include_in_schema=False)
def dashboard():
    return FileResponse(web_dir / 'index.html')


@app.get('/health/live', tags=['health'])
def live():
    return {'status': 'ok'}


@app.get('/health/ready', tags=['health'])
def ready():
    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        client = Redis.from_url(s.redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            client.ping()
        finally:
            client.close()
        get_store().ping()
    except Exception:
        logger.exception('Readiness probe failed')
        raise HTTPException(503, 'Database, Redis or object store unavailable') from None
    return {'status': 'ready'}


@app.get('/internal/metrics', include_in_schema=False)
def metrics(request: Request):
    token = request.headers.get('X-Metrics-Token', '')
    if not s.metrics_token or not secrets.compare_digest(token, s.metrics_token):
        raise HTTPException(404, 'Not found')
    try:
        from app.database import SessionLocal
        with SessionLocal() as db:
            counts = dict(db.execute(select(DocumentVersion.status, func.count()).group_by(DocumentVersion.status)))
        for status in ('queued', 'processing', 'ready', 'failed', 'superseded'):
            version_states.labels(status=status).set(counts.get(status, 0))
    except Exception:
        logger.exception('Ingestion status metrics unavailable')
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
