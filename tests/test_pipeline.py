"""Embeddings and LLM are mocked; real FAISS serialization and ingestion state machines run."""
from types import SimpleNamespace


def test_ingestion_promotion_retrieval_and_version_update(client, headers, monkeypatch):
    from langchain_core.runnables import RunnableLambda
    from app import rag, vector_index
    from app.tasks import ingest_version
    from app.database import SessionLocal
    from app.models import DocumentRecord, DocumentVersion

    class FakeEmbeddings:
        def embed_documents(self, texts):
            return [[1., 0., 0.] for _ in texts]
        def embed_query(self, question):
            return [1., 0., 0.]

    monkeypatch.setattr(vector_index, 'embedding_client', lambda: FakeEmbeddings())
    monkeypatch.setattr(rag, 'chat_model', lambda: RunnableLambda(
        lambda messages: SimpleNamespace(content='Returns accepted within 30 days [S1].',
                                         usage_metadata={'input_tokens': 25, 'output_tokens': 12})))
    upload = client.post('/v1/documents', headers=headers['alpha'], files={
        'file': ('returns.md', b'Returns accepted within 30 days. Refunds are processed by the team.', 'text/markdown')})
    assert upload.status_code == 202, upload.text
    doc_id = upload.json()['id']
    version_id = upload.json()['pending_version_id']
    result = ingest_version.apply(args=(version_id,)).get()
    assert result['result'] == 'processed'
    assert ingest_version.apply(args=(version_id,)).get()['result'] == 'not_claimed'
    with SessionLocal() as db:
        doc = db.get(DocumentRecord, doc_id)
        assert doc.active_version_id == version_id
        version = db.get(DocumentVersion, version_id)
        assert version.status == 'ready' and version.index_sha256 and version.index_metadata_sha256
    answer = client.post('/v1/qa/query', headers=headers['alpha'], json={
        'question': 'When can I return an item?', 'document_ids': [doc_id]})
    assert answer.status_code == 200, answer.text
    assert answer.json()['grounded'] is True
    assert answer.json()['citations_valid'] is True
    assert answer.json()['sources'][0]['document_id'] == doc_id
    assert client.get('/v1/workspace/usage', headers=headers['alpha']).json()['usage']['input_tokens'] == 25
    upload2 = client.post(f'/v1/documents/{doc_id}/versions', headers=headers['alpha'], files={
        'file': ('returns-updated.md', b'Return policy updated to 45 days.', 'text/markdown')})
    assert upload2.status_code == 202
    assert upload2.json()['active_version_id'] == version_id
    result = ingest_version.apply(args=(upload2.json()['pending_version_id'],)).get()
    assert result['result'] == 'processed'
    with SessionLocal() as db:
        doc = db.get(DocumentRecord, doc_id)
        assert doc.active_version_id == upload2.json()['pending_version_id']


def test_failed_new_version_preserves_previous_active_revision(client, headers):
    from app.database import SessionLocal
    from app.models import DocumentRecord, DocumentVersion
    from app.tasks import ingest_version
    first = client.post('/v1/documents', headers=headers['alpha'], files={
        'file': ('good.txt', b'The first version of a policy is valid.', 'text/plain')})
    doc_id = first.json()['id']
    with SessionLocal.begin() as db:
        doc = db.get(DocumentRecord, doc_id)
        version = db.get(DocumentVersion, doc.pending_version_id)
        version.status = 'ready'
        doc.active_version_id = version.id
        doc.pending_version_id = None
        doc.status = 'ready'
        previous_id = version.id
    new = client.post(f'/v1/documents/{doc_id}/versions', headers=headers['alpha'], files={
        'file': ('bad.txt', b'\xff\xfenot utf-8', 'text/plain')})
    assert new.status_code == 202
    invalid = ingest_version.apply(args=(new.json()['pending_version_id'],)).get()
    assert invalid['result'] == 'failed'
    with SessionLocal() as db:
        doc = db.get(DocumentRecord, doc_id)
        assert doc.status == 'ready'
        assert doc.active_version_id == previous_id
        assert db.get(DocumentVersion, new.json()['pending_version_id']).status == 'failed'


def test_abstain_without_documents(client, headers):
    response = client.post('/v1/qa/query', headers=headers['alpha'], json={'question': 'What is the policy?'})
    assert response.status_code == 200
    assert response.json()['grounded'] is False
    assert response.json()['sources'] == []
