"""No third-party credentials: verifiable signup and quota compensation."""


def test_email_verified_signup(client, monkeypatch):
    from app.config import get_settings
    from app.api import auth
    sent = {}
    monkeypatch.setattr(get_settings(), 'smtp_enabled', True)
    monkeypatch.setattr(auth, 'send_access_link', lambda email, token, kind: sent.update(
        email=email, token=token, kind=kind))
    result = client.post('/v1/auth/register/start', json={
        'workspace_name': 'A New Company', 'workspace_slug': 'newcompany',
        'email': 'chief@example.org', 'password': 'Strong-password-that-is-long'})
    assert result.status_code == 202, result.text
    assert sent['email'] == 'chief@example.org' and sent['kind'] == 'verify'
    verification = client.post('/v1/auth/register/verify', json={'token': sent['token']})
    assert verification.status_code == 200, verification.text
    assert client.post('/v1/auth/register/verify', json={'token': sent['token']}).status_code == 400
    headers = {'Authorization': 'Bearer ' + verification.json()['access_token']}
    user = client.get('/v1/auth/me', headers=headers)
    assert user.status_code == 200
    assert user.json()['role'] == 'owner'


def test_invite_is_one_time(client, headers):
    new = client.post('/v1/auth/invitations', headers=headers['alpha'],
                      json={'email': 'teammate@example.org', 'role': 'viewer'})
    assert new.status_code == 201, new.text
    token = new.json()['invitation_token']
    accepted = client.post('/v1/auth/invitations/accept', json={
        'token': token, 'password': 'A-very-secure-password-for-user'})
    assert accepted.status_code == 200, accepted.text
    assert client.post('/v1/auth/invitations/accept', json={
        'token': token, 'password': 'A-very-secure-password-for-user'}).status_code == 400
    viewer_headers = {'Authorization': 'Bearer ' + accepted.json()['access_token']}
    assert client.get('/v1/documents', headers=viewer_headers).status_code == 200
    assert client.post('/v1/documents', headers=viewer_headers,
                       files={'file': ('deny.txt', b'no', 'text/plain')}).status_code == 403


def test_failed_questions_are_not_counted(client, headers, monkeypatch):
    from app.database import SessionLocal
    from app.models import DocumentRecord, DocumentVersion
    from app.api import qa
    response = client.post('/v1/documents', headers=headers['alpha'],
                           files={'file': ('policy.md', b'The policy is on Tuesday.', 'text/markdown')})
    doc_id = response.json()['id']
    with SessionLocal.begin() as db:
        doc = db.get(DocumentRecord, doc_id)
        ver = db.get(DocumentVersion, doc.pending_version_id)
        doc.active_version_id = ver.id
        doc.pending_version_id = None
        doc.status = 'ready'
        ver.status = 'ready'
    monkeypatch.setattr(qa, 'answer_question', lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('Mock model outage')))
    result = client.post('/v1/qa/query', headers=headers['alpha'],
                         json={'question': 'What is the current policy?', 'document_ids': [doc_id]})
    assert result.status_code == 502, result.text
    usage = client.get('/v1/workspace/usage', headers=headers['alpha']).json()['usage']
    assert usage['queries'] == 0
