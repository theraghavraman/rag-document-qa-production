"""Immutable, checksum-verified FAISS shards plus lexical retrieval and optional reranking."""
import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

import faiss
import numpy as np
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from rank_bm25 import BM25Okapi

from app.config import get_settings
from app.models import DocumentVersion
from app.storage import get_store, index_prefix

logger = logging.getLogger(__name__)
_cache_lock = threading.RLock()
_token_re = re.compile(r'\b\w+\b', re.UNICODE)


class EmbeddingModelMismatch(ValueError):
    pass


def embedding_client():
    s = get_settings()
    if not s.openai_api_key:
        raise RuntimeError('OPENAI_API_KEY not configured for embedding model')
    kwargs = {'model': s.openai_embedding_model, 'api_key': s.openai_api_key,
              'max_retries': 3, 'request_timeout': 45}
    if s.openai_base_url:
        kwargs['base_url'] = s.openai_base_url
    return OpenAIEmbeddings(**kwargs)


def normalize(vectors: list[list[float]]) -> np.ndarray:
    data = np.asarray(vectors, dtype=np.float32)
    if data.ndim != 2 or not len(data) or not np.isfinite(data).all():
        raise ValueError('Invalid embedding vectors')
    if (np.linalg.norm(data, axis=1) < 1e-9).any():
        raise ValueError('Empty embedding vectors')
    faiss.normalize_L2(data)
    return np.ascontiguousarray(data)


def write_index(chunks: list[Document], directory: Path) -> tuple[str, str]:
    s = get_settings()
    client = embedding_client()
    vectors = []
    for start in range(0, len(chunks), s.embedding_batch_size):
        vectors.extend(client.embed_documents([c.page_content for c in chunks[start:start + s.embedding_batch_size]]))
    data = normalize(vectors)
    if data.shape[0] != len(chunks):
        raise ValueError('Embedding count mismatch')
    directory.mkdir(parents=True, exist_ok=True)
    index = faiss.IndexFlatIP(data.shape[1])
    index.add(data)
    faiss.write_index(index, str(directory / 'index.faiss'))
    metadata = {'embedding_model': s.openai_embedding_model, 'dimension': data.shape[1],
                'chunks': [{'text': c.page_content, 'page': c.metadata.get('page'),
                            'chunk_index': c.metadata['chunk_index']} for c in chunks]}
    (directory / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
    return (hashlib.sha256((directory / 'index.faiss').read_bytes()).hexdigest(),
            hashlib.sha256((directory / 'metadata.json').read_bytes()).hexdigest())


def upload_index(directory: Path, prefix: str):
    store = get_store()
    # Publish data before updating DB active pointer; incomplete indexes remain invisible.
    store.put_file(directory / 'index.faiss', f'{prefix}/index.faiss')
    store.put_file(directory / 'metadata.json', f'{prefix}/metadata.json')


@lru_cache(maxsize=256)
def _cached_digest(filename: str, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with open(filename, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path: Path, expected: str | None) -> bool:
    if not path.exists() or not expected:
        return False
    stats = path.stat()
    return _cached_digest(str(path), stats.st_size, stats.st_mtime_ns) == expected


def _cache_dir(version: DocumentVersion) -> Path:
    return get_settings().data_dir / 'cache' / version.tenant_id / version.id


def _ensure_index(version: DocumentVersion) -> Path:
    target = _cache_dir(version)
    with _cache_lock:
        index_file = target / 'index.faiss'
        if _matches(index_file, version.index_sha256) and \
                _matches(target / 'metadata.json', version.index_metadata_sha256):
            return target
        if not version.index_prefix or not version.index_sha256 or not version.index_metadata_sha256:
            raise FileNotFoundError('Version has no published index')
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as staging:
            folder = Path(staging)
            get_store().get_file(version.index_prefix + '/index.faiss', folder / 'index.faiss')
            get_store().get_file(version.index_prefix + '/metadata.json', folder / 'metadata.json')
            if not _matches(folder / 'index.faiss', version.index_sha256) or not \
                    _matches(folder / 'metadata.json', version.index_metadata_sha256):
                raise RuntimeError('Vector index or metadata integrity check failed')
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(folder, target)
        return target


@lru_cache(maxsize=32)
def _loaded(cache_path: str, checksum: str, metadata_checksum: str):
    path = Path(cache_path)
    metadata = json.loads((path / 'metadata.json').read_text(encoding='utf-8'))
    faiss_index = faiss.read_index(str(path / 'index.faiss'))
    if faiss_index.ntotal != len(metadata['chunks']) or faiss_index.d != metadata['dimension']:
        raise RuntimeError('FAISS metadata/index mismatch')
    return faiss_index, metadata


def embed_question(question: str) -> np.ndarray:
    return normalize([embedding_client().embed_query(question)])


def search_index(version: DocumentVersion, query_vector: np.ndarray,
                 question: str, top_k: int) -> list[dict]:
    directory = _ensure_index(version)
    index, meta = _loaded(str(directory), version.index_sha256, version.index_metadata_sha256)
    if meta['embedding_model'] != get_settings().openai_embedding_model or index.d != query_vector.shape[1]:
        raise EmbeddingModelMismatch('Embedding model changed; reindex affected documents')
    results: dict[int, dict] = {}
    similarities, indices = index.search(query_vector, min(top_k * 3, index.ntotal))
    for rank, (idx, sim) in enumerate(zip(indices[0], similarities[0]), 1):
        if idx < 0:
            continue
        score = float(sim)
        results[int(idx)] = {**meta['chunks'][int(idx)], 'similarity': score,
                             'rank_score': 1 / (60 + rank)}
    if get_settings().retrieval_mode == 'hybrid':
        corpus = [_token_re.findall(x['text'].lower()) or [''] for x in meta['chunks']]
        lexical = BM25Okapi(corpus)
        scores = lexical.get_scores(_token_re.findall(question.lower()))
        for rank, idx in enumerate(np.argsort(-scores)[:min(top_k * 3, len(scores))], 1):
            if scores[int(idx)] <= 0:
                continue
            idx = int(idx)
            if idx not in results:
                results[idx] = {**meta['chunks'][idx], 'similarity': -1.0, 'rank_score': 0.0}
            results[idx]['rank_score'] += 1 / (60 + rank)
            results[idx]['lexical_hit'] = True
    return sorted(results.values(), key=lambda x: (x['rank_score'], x['similarity']), reverse=True)[:top_k * 3]


@lru_cache(maxsize=1)
def _cross_encoder():
    from sentence_transformers import CrossEncoder
    return CrossEncoder(get_settings().reranker_model)


def rerank(question: str, matches: list[dict]) -> list[dict]:
    if get_settings().reranker != 'cross-encoder' or not matches:
        return sorted(matches, key=lambda x: (x['rank_score'], x['similarity']), reverse=True)
    scores = _cross_encoder().predict([(question, m['text'][:2400]) for m in matches])
    for match, score in zip(matches, scores):
        match['rank_score'] = float(score)
    return sorted(matches, key=lambda x: x['rank_score'], reverse=True)
