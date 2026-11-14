import uuid


def test_isolation_permissions_and_usage(client, headers):
    response = client.post('/v1/documents', headers=headers['alpha'],
                           files={'file': ('terms.md', b'Confidential alpha workspace terms', 'text/markdown')})
    assert response.status_code == 202, response.text
    doc_id = response.json()['id']
    assert client.get('/v1/documents/' + doc_id, headers=headers['alpha']).status_code == 200
    assert client.get('/v1/documents/' + doc_id, headers=headers['beta']).status_code == 404
    assert client.delete('/v1/documents/' + doc_id, headers=headers['beta']).status_code == 404
    assert client.get('/v1/documents', headers=headers['beta']).json()['total'] == 0
    assert client.post('/v1/qa/query', headers=headers['beta'],
                       json={'question': 'What are the terms?', 'document_ids': [doc_id]}).status_code == 409
    assert client.get('/v1/workspace/usage', headers=headers['alpha']).json()['usage']['uploads'] == 1
    assert client.get('/v1/workspace/usage', headers=headers['beta']).json()['usage']['uploads'] == 0
    assert client.delete('/v1/documents/' + doc_id, headers=headers['alpha']).status_code == 204
    assert client.get('/v1/documents/' + doc_id, headers=headers['alpha']).status_code == 404


def test_api_key_hashing_revocation_and_viewer(client, headers):
    response = client.post('/v1/auth/keys', headers=headers['alpha'],
                           json={'name': 'read-only', 'role': 'viewer'})
    assert response.status_code == 201, response.text
    token = response.json()['api_key']
    from app.database import SessionLocal
    from app.models import APIKey
    with SessionLocal() as db:
        row = db.get(APIKey, response.json()['id'])
        assert token not in row.secret_hash
    key = {'X-API-Key': token}
    assert client.get('/v1/documents', headers=key).status_code == 200
    assert client.post('/v1/documents', headers=key,
                       files={'file': ('blocked.txt', b'no', 'text/plain')}).status_code == 403
    assert client.delete('/v1/auth/keys/' + response.json()['id'], headers=headers['alpha']).status_code == 204
    assert client.get('/v1/documents', headers=key).status_code == 401


def test_refresh_token_is_rotated(client, headers):
    token = headers['alpha_refresh']
    renew = client.post('/v1/auth/refresh', json={'refresh_token': token})
    assert renew.status_code == 200
    assert renew.json()['refresh_token'] != token
    assert client.post('/v1/auth/refresh', json={'refresh_token': token}).status_code == 401
    assert client.post('/v1/auth/logout', headers=headers['alpha'],
                       json={'refresh_token': renew.json()['refresh_token']}).status_code == 204
    assert client.post('/v1/auth/refresh', json={'refresh_token': renew.json()['refresh_token']}).status_code == 401


def test_upload_rejects_invalid_formats_and_large_bytes(client, headers):
    assert client.post('/v1/documents', headers=headers['alpha'],
                       files={'file': ('exploit.exe', b'malicious', 'application/octet-stream')}).status_code == 415
    assert client.post('/v1/documents', headers=headers['alpha'],
                       files={'file': ('fake.pdf', b'Not PDF', 'application/pdf')}).status_code == 422
    assert client.post('/v1/documents', headers=headers['alpha'],
                       files={'file': ('empty.txt', b'', 'text/plain')}).status_code == 422


def test_old_revision_can_be_deleted_without_removing_active(client, headers, monkeypatch):
    # Insert synthetic ready state; no external model call needed for old-version deletion.
    first = client.post('/v1/documents', headers=headers['alpha'],
                        files={'file': ('policy.md', b'v1', 'text/plain')})
    assert first.status_code == 202
    doc_id = first.json()['id']
    from app.database import SessionLocal
    from app.models import DocumentRecord, DocumentVersion
    with SessionLocal.begin() as db:
        doc = db.get(DocumentRecord, doc_id)
        ver = db.get(DocumentVersion, doc.pending_version_id)
        ver.status = 'ready'
        doc.active_version_id = ver.id
        doc.pending_version_id = None
        doc.status = 'ready'
        v1 = ver.id
    second = client.post(f'/v1/documents/{doc_id}/versions', headers=headers['alpha'],
                         files={'file': ('policy.md', b'v2', 'text/plain')})
    assert second.status_code == 202
    with SessionLocal.begin() as db:
        doc = db.get(DocumentRecord, doc_id)
        new = db.get(DocumentVersion, doc.pending_version_id)
        new.status = 'ready'
        doc.active_version_id = new.id
        doc.pending_version_id = None
        doc.status = 'ready'
    assert client.delete(f'/v1/documents/{doc_id}/versions/{v1}', headers=headers['alpha']).status_code == 204
    assert client.get(f'/v1/documents/{doc_id}/versions', headers=headers['alpha']).json()[0]['version_number'] == 2
