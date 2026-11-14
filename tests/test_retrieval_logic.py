"""Regression checks for bounded context and lexical-only evidence rescue."""
from types import SimpleNamespace


def test_hybrid_keyword_hits_survive_dense_threshold_and_sources_match_prompt(monkeypatch):
    from app import rag
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, 'max_context_chars', 500)
    monkeypatch.setattr(settings, 'retrieval_mode', 'hybrid')
    monkeypatch.setattr(settings, 'min_similarity', .75)
    monkeypatch.setattr(rag, 'embed_question', lambda question: None)
    first = {'text': 'A' * 3000, 'page': 1, 'chunk_index': 0,
             'similarity': -1.0, 'lexical_hit': True, 'rank_score': .01}
    second = {'text': 'B' * 3000, 'page': 2, 'chunk_index': 1,
              'similarity': .94, 'rank_score': .02}
    monkeypatch.setattr(rag, 'search_index', lambda *args: [first, second])
    monkeypatch.setattr(rag, 'rerank', lambda _question, matches: matches)
    document = SimpleNamespace(id='doc-one')
    version = SimpleNamespace(id='ver-one', filename='policy.md')
    sources, context = rag._retrieve('rare lexical keyword', [(document, version)], 5, None)
    assert len(sources) == 1
    assert sources[0].similarity == -1.0
    assert sources[0].source_id == 'S1'
    assert sources[0].document_id == 'doc-one'
    assert '[S1]' in context and '[S2]' not in context
    assert len(context) <= 500
