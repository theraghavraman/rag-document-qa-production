"""Full suite runs with project requirements installed, no Redis/S3/LLM network calls."""
import os
import tempfile
from pathlib import Path

import pytest

ROOT = Path(tempfile.mkdtemp(prefix='rag-pytest-'))
os.environ.update({'APP_ENV': 'test', 'DATABASE_URL': f'sqlite:///{ROOT / "test.db"}',
                   'DATA_DIR': str(ROOT / 'data'), 'AUTO_CREATE_TABLES': 'true',
                   'STORAGE_BACKEND': 'local', 'ENABLE_OCR': 'false',
                   'ENABLE_PUBLIC_SIGNUP': 'true', 'JWT_SECRET': 'pytest-jwt-secret-not-for-production',
                   'API_KEY_PEPPER': 'pytest-pepper-secret-not-for-production',
                   'OPENAI_API_KEY': 'sk-placeholder-not-used'})


@pytest.fixture(scope='session', autouse=True)
def configure_database():
    from app.database import Base, engine
    from app import models  # noqa: F401
    from app.storage import create_dirs
    create_dirs()
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_db(configure_database):
    from app.database import Base, SessionLocal
    from app.storage import get_store
    import shutil
    with SessionLocal.begin() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
    get_store.cache_clear()
    shutil.rmtree(ROOT / 'data' / 'objects', ignore_errors=True)
    shutil.rmtree(ROOT / 'data' / 'cache', ignore_errors=True)


@pytest.fixture
def users():
    from app.database import SessionLocal
    from app.models import Tenant, User
    from app.security import password_hash
    with SessionLocal.begin() as db:
        for slug in ('alpha', 'beta'):
            tenant = Tenant(name=slug.title(), slug=slug)
            db.add(tenant)
            db.flush()
            db.add(User(tenant_id=tenant.id, email='owner@example.com', role='owner',
                        password_hash=password_hash('This-is-a-strong-password-123')))
    return ['alpha', 'beta']


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import documents
    from app.main import app
    monkeypatch.setattr(documents, 'publish_version', lambda version_id: True)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def headers(client, users):
    result = {}
    for slug in users:
        reply = client.post('/v1/auth/login', json={
            'workspace_slug': slug, 'email': 'owner@example.com',
            'password': 'This-is-a-strong-password-123'})
        assert reply.status_code == 200, reply.text
        result[slug] = {'Authorization': 'Bearer ' + reply.json()['access_token']}
        result[slug + '_refresh'] = reply.json()['refresh_token']
    return result
