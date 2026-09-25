from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi import Query as QueryParam
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.middleware.auth import RequireOperator, get_db
from app.middleware.rate_limit import get_redis, record_token_usage, token_budget_dependency
from app.models.db import Query, Response, User
from app.models.schemas import Citation, QueryHistoryItem, QueryRequest
from app.services import audit_log, semantic_cache
from app.services.embedder import embed_single, embed_texts
from app.services.hitl import classify_risk, queue_for_review
from app.services.llm import (
    build_context_block,
    estimate_confidence,
    expand_query,
    extract_citations,
    stream_answer,
    user_facing_llm_error,
)
from app.services.retriever import rerank_chunks, retrieve_chunks

router = APIRouter(prefix="/query", tags=["query"])
logger = logging.getLogger(__name__)

HELD_MESSAGE = (
    "This answer contains operational recommendations or low-confidence findings and is being held "
    "for review by a qualified engineer. It will appear here once approved."
)
REJECTED_MESSAGE = "This answer was rejected by the reviewing engineer and has been withheld."

# Seconds between SSE keep-alive comments while the full answer is generated server-side.
HEARTBEAT_SECONDS = 10.0
# Size of the pieces a finished answer is replayed in, so the client still renders progressively.
REPLAY_CHUNK_CHARS = 48


# Personal data only. Presidio's defaults also tag ORGANIZATION, LOCATION and DATE_TIME,
# which would redact segment IDs (e.g. "SEG-TX-4B"), cities and inspection dates —
# exactly what integrity questions are about.
PII_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_SSN",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "IP_ADDRESS",
]


@lru_cache(maxsize=1)
def _presidio_engines():
    """Build Presidio once; returns None if it (or its spaCy model) isn't available."""
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine

        nlp = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": settings.pii_spacy_model}],
            }
        ).create_engine()
        return AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"]), AnonymizerEngine()
    except ImportError:
        return None
    except BaseException:  # spaCy raises SystemExit when a model is missing and can't be downloaded
        logger.warning("PII scrubbing disabled: could not load spaCy model %s", settings.pii_spacy_model, exc_info=True)
        return None


def _scrub_pii(text: str) -> str:
    """Replace personal data in the question before it is embedded or sent to the LLM."""
    engines = _presidio_engines()
    if engines is None:
        return text
    analyzer, anonymizer = engines
    try:
        results = analyzer.analyze(text=text, language="en", entities=PII_ENTITIES)
        return anonymizer.anonymize(text=text, analyzer_results=results).text
    except Exception:
        logger.warning("PII scrub failed; using raw question", exc_info=True)
        return text


def _detect_language(text: str) -> str:
    try:
        from langdetect import detect

        return detect(text)
    except Exception:
        return "en"


def _sse(**data: object) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _operator_visible_answer(q: Query, resp: Response) -> str:
    """Never expose an unreviewed or rejected answer to the asker."""
    if q.status == "UNDER_REVIEW":
        return HELD_MESSAGE
    if q.status == "REJECTED":
        return REJECTED_MESSAGE
    return resp.answer_text


@router.get("/history", response_model=list[QueryHistoryItem])
async def get_query_history(
    user: Annotated[User, Depends(RequireOperator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, QueryParam(ge=1, le=200)] = 50,
) -> list[QueryHistoryItem]:
    """Return the current user's query history, oldest-first, with review outcomes."""
    stmt = (
        select(Query)
        .where(Query.user_id == user.id)
        .options(selectinload(Query.response).selectinload(Response.hitl_review))
        .order_by(Query.query_ts.desc())
        .limit(limit)
    )
    queries = (await db.execute(stmt)).scalars().all()

    items: list[QueryHistoryItem] = []
    for q in reversed(queries):  # oldest first for display
        resp = q.response
        if not resp:
            continue
        review = resp.hitl_review
        citations: list[Citation] = []
        for c in resp.citations_json or []:
            try:
                citations.append(Citation(**c))
            except Exception:
                logger.warning("Skipping malformed citation on query %s", q.id, exc_info=True)
        items.append(
            QueryHistoryItem(
                query_id=q.id,
                question=q.question_raw,
                asked_at=q.query_ts,
                status=q.status,
                hitl_required=q.hitl_required,
                answer_text=_operator_visible_answer(q, resp),
                final_text=review.final_text if review else None,
                decision=review.decision if review else None,
                reason=review.reason if review else None,
                reviewed_at=review.reviewed_at if review else None,
                citations=citations,
                confidence_score=resp.confidence_score,
            )
        )
    return items


async def _generate_with_heartbeat(
    question: str, context_block: str, language: str, usage: dict[str, int]
) -> AsyncIterator[str | None]:
    """Collect the full answer; yields None as a heartbeat while waiting, then the answer."""
    parts: list[str] = []

    async def _collect() -> None:
        async for token in stream_answer(question, context_block, language=language, usage=usage):
            parts.append(token)

    task = asyncio.create_task(_collect())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
            if done:
                task.result()  # re-raise generation errors
                break
            yield None
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    yield "".join(parts)


@router.post("")
async def query_endpoint(
    request_body: QueryRequest,
    request: Request,
    user: Annotated[User, Depends(RequireOperator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _budget: Annotated[None, Depends(token_budget_dependency)],
) -> StreamingResponse:
    """
    Main RAG query endpoint, answered via Server-Sent Events.

    The full answer is generated and risk-classified server-side before any of it is
    sent: low-risk answers are then streamed as `delta` events; answers routed to HITL
    review are withheld and the client receives HELD_MESSAGE with `hitl_required: true`.
    The last event carries `done: true` and the citations. `: ping` comments keep the
    connection alive during generation.
    """
    redis = get_redis()
    query_id = str(uuid.uuid4())
    ip = request.client.host if request.client else None
    # Plain values: ORM objects are expired by the rollback in the error path.
    user_id = user.id

    async def event_stream() -> AsyncIterator[str]:
        start_time = time.perf_counter()
        db_query: Query | None = None
        base = {"query_id": query_id}

        try:
            detected_lang = _detect_language(request_body.question)
            language = request_body.language or detected_lang
            clean_question = await asyncio.to_thread(_scrub_pii, request_body.question)
            filters_json = request_body.filters.model_dump() if request_body.filters else None
            scope = semantic_cache.cache_scope(language, filters_json)

            query_embedding = await embed_single(clean_question)

            db_query = Query(
                id=uuid.UUID(query_id),
                session_id=request_body.session_id,
                user_id=user_id,
                question_raw=request_body.question,
                question_lang=language,
                filters_json=filters_json,
                hitl_required=False,
                status="PROCESSING",
            )
            db.add(db_query)
            await db.commit()

            # Only reviewed-safe (delivered) answers are ever cached, so a hit needs no HITL gate.
            cached = await semantic_cache.lookup(redis, scope, query_embedding)
            if cached:
                db.add(
                    Response(
                        id=uuid.uuid4(),
                        query_id=db_query.id,
                        answer_text=cached["answer"],
                        citations_json=cached.get("citations", []),
                        confidence_score=cached.get("confidence", 1.0),
                        risk_level="LOW",
                        model_version=f"cache:{cached.get('model_version', settings.llm_model)}",
                        latency_ms=int((time.perf_counter() - start_time) * 1000),
                    )
                )
                db_query.status = "DELIVERED"
                await db.commit()
                await audit_log.log_event(
                    db,
                    event_type="QUERY_COMPLETED",
                    actor_id=str(user_id),
                    target_id=query_id,
                    target_type="query",
                    payload={"risk_level": "LOW", "hitl_required": False, "cached": True},
                    ip_address=ip,
                )
                citations_out = cached.get("citations", [])
                yield _sse(**base, delta=cached["answer"], citations=[], hitl_required=False, cached=True)
                yield _sse(**base, delta="", citations=citations_out, hitl_required=False, cached=True, done=True)
                return

            # Query expansion is best-effort: retrieval still works on the original question.
            try:
                variants = await expand_query(clean_question)
                variant_embeddings = await embed_texts(variants) if variants else []
            except Exception:
                logger.warning("Query expansion failed; continuing without variants", exc_info=True)
                variant_embeddings = []

            all_chunks: list[dict] = []
            seen_ids: set[str] = set()
            for emb in [query_embedding, *variant_embeddings]:
                for chunk in await retrieve_chunks(db, emb, request_body.filters, top_k=settings.top_k_retrieval):
                    if chunk["id"] not in seen_ids:
                        seen_ids.add(chunk["id"])
                        all_chunks.append(chunk)

            reranked = await asyncio.to_thread(rerank_chunks, clean_question, all_chunks, settings.top_k_rerank)
            context_block = build_context_block(reranked)

            usage: dict[str, int] = {}
            full_answer = ""
            async for item in _generate_with_heartbeat(clean_question, context_block, language, usage):
                if item is None:
                    yield ": ping\n\n"
                else:
                    full_answer = item

            confidence = estimate_confidence(full_answer, reranked)
            citations = [c.model_dump() for c in extract_citations(full_answer, reranked)]
            risk_level, hitl_required = classify_risk(full_answer, confidence)

            db_response = Response(
                id=uuid.uuid4(),
                query_id=db_query.id,
                answer_text=full_answer,
                citations_json=citations,
                confidence_score=confidence,
                risk_level=risk_level,
                model_version=settings.llm_model,
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
                latency_ms=int((time.perf_counter() - start_time) * 1000),
            )
            db.add(db_response)
            await db.flush()

            if hitl_required:
                await queue_for_review(db, db_response, db_query)
            else:
                db_query.status = "DELIVERED"
                await db.commit()
                await semantic_cache.store(
                    redis,
                    scope,
                    query_id,
                    query_embedding,
                    {
                        "answer": full_answer,
                        "citations": citations,
                        "confidence": confidence,
                        "model_version": settings.llm_model,
                    },
                )

            # Enforced before the next request (token_budget_dependency); never fails this one.
            await record_token_usage(str(user_id), usage.get("input_tokens", 0) + usage.get("output_tokens", 0))

            await audit_log.log_event(
                db,
                event_type="QUERY_COMPLETED",
                actor_id=str(user_id),
                target_id=query_id,
                target_type="query",
                payload={"risk_level": risk_level, "hitl_required": hitl_required, "confidence": confidence},
                ip_address=ip,
            )

            if hitl_required:
                yield _sse(**base, delta=HELD_MESSAGE, citations=[], hitl_required=True, held=True)
            else:
                for i in range(0, len(full_answer), REPLAY_CHUNK_CHARS):
                    yield _sse(**base, delta=full_answer[i : i + REPLAY_CHUNK_CHARS], citations=[], hitl_required=False)
            yield _sse(**base, delta="", citations=citations, hitl_required=hitl_required, done=True)

        except Exception as exc:
            logger.exception("Query %s failed", query_id)
            await db.rollback()
            if db_query is not None:
                db_query = await db.get(Query, uuid.UUID(query_id))
                if db_query is not None:
                    db_query.status = "FAILED"
                    await db.commit()
            await audit_log.log_event(
                db,
                event_type="QUERY_FAILED",
                actor_id=str(user_id),
                target_id=query_id,
                target_type="query",
                payload={"error_type": type(exc).__name__},
                ip_address=ip,
            )
            yield _sse(**base, delta=user_facing_llm_error(exc), citations=[], hitl_required=False, error=True)
            yield _sse(**base, delta="", citations=[], hitl_required=False, done=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
