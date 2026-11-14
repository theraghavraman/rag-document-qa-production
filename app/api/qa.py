"""Bounded, tenant-filtered Q&A; never search another tenant's index."""
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import DocumentRecord, DocumentVersion
from app.rag import answer_question
from app.schemas import QueryIn, QueryOut
from app.security import Principal, require_role
from app.services.usage import audit, record_tokens, refund_failed_query, reserve_query
from app.vector_index import EmbeddingModelMismatch

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/v1/qa', tags=['question answering'])


@router.post('/query', response_model=QueryOut)
async def query(request: QueryIn, principal: Principal = Depends(require_role('viewer')),
          db: Session = Depends(get_db)):
    s = get_settings()
    if request.document_ids is not None:
        if len(request.document_ids) > s.max_query_documents or len(set(request.document_ids)) != len(request.document_ids):
            raise HTTPException(422, 'Too many or duplicate document IDs')
        try:
            requested = {str(uuid.UUID(x)) for x in request.document_ids}
        except ValueError:
            raise HTTPException(422, 'Invalid document UUID') from None
    else:
        requested = None
    statement = select(DocumentRecord, DocumentVersion).join(
        DocumentVersion, DocumentRecord.active_version_id == DocumentVersion.id).where(
            DocumentRecord.tenant_id == principal.tenant_id,
            DocumentVersion.tenant_id == principal.tenant_id,
            DocumentRecord.deleted_at.is_(None),
            DocumentRecord.status == 'ready', DocumentVersion.status == 'ready')
    if requested is not None:
        statement = statement.where(DocumentRecord.id.in_(requested))
    records = list(db.execute(statement.limit(s.max_query_documents + 1)).all())
    if len(records) > s.max_query_documents:
        raise HTTPException(422, 'Too many documents; specify document_ids')
    if requested and {doc.id for doc, _ in records} != requested:
        raise HTTPException(409, 'Some documents are unavailable or not ready')
    reservation_id = reserve_query(db, principal.tenant_id, principal.actor_id)
    audit(db, principal.tenant_id, principal.actor_id, 'qa.query')
    db.commit()
    try:
        response = await answer_question(request.question.strip(), records, request.top_k, request.min_similarity)
    except EmbeddingModelMismatch as exc:
        refund_failed_query(db, principal.tenant_id, reservation_id)
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    except (FileNotFoundError, OSError):
        refund_failed_query(db, principal.tenant_id, reservation_id)
        db.commit()
        logger.exception('Index storage unavailable')
        raise HTTPException(503, 'Index temporarily unavailable') from None
    except Exception:
        refund_failed_query(db, principal.tenant_id, reservation_id)
        db.commit()
        logger.exception('Retrieval or LLM provider unavailable')
        raise HTTPException(502, 'Retrieval or LLM provider unavailable') from None
    record_tokens(db, principal.tenant_id, response.input_tokens, response.output_tokens)
    db.commit()
    return response
