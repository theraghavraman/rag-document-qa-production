"""Dependency-light invariants that can run without Redis, Celery or FAISS."""
from pathlib import Path
from types import SimpleNamespace
import pytest


def test_production_fails_closed_when_insecure():
    from app.config import Settings
    with pytest.raises(ValueError):
        Settings(_env_file=None, app_env='production')


def test_storage_path_is_restricted(tmp_path):
    from app.storage import LocalStore, source_key, safe_filename
    store = LocalStore(tmp_path)
    payload = tmp_path / 'test.txt'
    payload.write_text('no external links allowed')
    key = source_key('tenant-uuid', 'doc-uuid', 'version-uuid', '.txt')
    store.put_file(payload, key)
    target = tmp_path / 'copy.txt'
    store.get_file(key, target)
    assert target.read_bytes() == payload.read_bytes()
    assert safe_filename('../../secret.txt') == 'secret.txt'
    with pytest.raises(ValueError):
        store.path('../escape')
    with pytest.raises(ValueError):
        store.delete_prefix('')


def test_storage_and_usage_quota_transactions():
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app.database import Base
    from app.models import Tenant, UsagePeriod
    from app.services.usage import reserve_upload, reserve_query
    engine = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        t = Tenant(slug='unit-test', name='Unit Test')
        db.add(t); db.flush()
        reserve_upload(db, t.id, 1024, True, 'actor')
        reserve_query(db, t.id, 'actor')
        db.commit()
        usage = db.scalar(select(UsagePeriod).where(UsagePeriod.tenant_id == t.id))
        assert usage.uploads == 1 and usage.queries == 1
        assert t.current_storage_bytes == 1024
    engine.dispose()


def test_ingress_body_limit_and_log_redaction(client):
    from app.main import _LOG_SECRET_RE
    scrubbed = _LOG_SECRET_RE.sub("[REDACTED]", "Authorization: Bearer secret_token_1234567890 and sk-1234567890abcdefghij")
    assert "secret_token_1234567890" not in scrubbed
    assert "sk-1234567890" not in scrubbed
    res = client.post("/v1/qa/query", content=b"{}", headers={"Content-Length": str(50 * 1024 * 1024), "Content-Type": "application/json"})
    assert res.status_code == 413
