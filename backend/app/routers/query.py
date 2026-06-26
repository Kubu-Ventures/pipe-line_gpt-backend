from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, AsyncIterator

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.middleware.auth import RequireOperator, get_db, get_current_user
from app.middleware.rate_limit import check_and_consume_tokens, get_redis
from app.models.db import Query, Response, User
from app.models.schemas import QueryRequest
from app.services import audit_log
from app.services.embedder import cosine_similarity, embed_single, embed_texts
from app.services.hitl import classify_risk, queue_for_review
from app.services.llm import (
    build_context_block,
    estimate_confidence,
    expand_query,
    extract_citations,
    stream_answer,
)
from app.services.retriever import rerank_chunks, retrieve_chunks

router = APIRouter(prefix="/query", tags=["query"])


def _scrub_pii(text: str) -> str:
    """Run Microsoft Presidio PII scrubber on user input."""
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine

        analyzer = AnalyzerEngine()
        anonymizer = AnonymizerEngine()
        results = analyzer.analyze(text=text, language="en")
        return anonymizer.anonymize(text=text, analyzer_results=results).text
    except Exception:
        return text


def _detect_language(text: str) -> str:
    try:
        from langdetect import detect

        return detect(text)
    except Exception:
        return "en"


async def _check_semantic_cache(redis: aioredis.Redis, query_embedding: list[float]) -> dict | None:
    """Return cached response if a sufficiently similar query was answered recently."""
    try:
        keys = await redis.keys("query_cache:*")
        for key in keys:
            cached_raw = await redis.get(key)
            if not cached_raw:
                continue
            cached = json.loads(cached_raw)
            cached_emb = cached.get("embedding", [])
            if not cached_emb:
                continue
            sim = cosine_similarity(query_embedding, cached_emb)
            if sim >= settings.semantic_cache_similarity:
                return cached
    except Exception:
        pass
    return None


async def _store_semantic_cache(
    redis: aioredis.Redis,
    query_id: str,
    embedding: list[float],
    response_data: dict,
) -> None:
    key = f"query_cache:{query_id}"
    payload = {"embedding": embedding, **response_data}
    try:
        await redis.setex(key, settings.semantic_cache_ttl_seconds, json.dumps(payload))
    except Exception:
        pass


@router.post("")
async def query_endpoint(
    request_body: QueryRequest,
    request: Request,
    user: Annotated[User, Depends(RequireOperator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """
    Main RAG query endpoint. Streams the answer via Server-Sent Events.
    Each SSE event is a JSON object: {delta, citations, hitl_required, query_id}.
    """
    redis = get_redis()
    query_id = str(uuid.uuid4())
    ip = request.client.host if request.client else None

    async def event_stream() -> AsyncIterator[str]:
        start_time = time.perf_counter()

        # 1. Language detection + PII scrub
        detected_lang = _detect_language(request_body.question)
        language = request_body.language or detected_lang
        clean_question = _scrub_pii(request_body.question)

        # 2. Embed question
        query_embedding = await embed_single(clean_question)

        # 2a. Semantic cache check
        cached = await _check_semantic_cache(redis, query_embedding)
        if cached:
            payload = json.dumps(
                {
                    "delta": cached.get("answer", ""),
                    "citations": cached.get("citations", []),
                    "hitl_required": False,
                    "query_id": query_id,
                    "cached": True,
                }
            )
            yield f"data: {payload}\n\n"
            return

        # 3. Query expansion
        variants = await expand_query(clean_question)
        variant_embeddings = await embed_texts(variants) if variants else []
        all_embeddings = [query_embedding] + variant_embeddings

        # 4. Vector retrieval — union results across all query variants, deduplicate by chunk id
        all_chunks: list[dict] = []
        seen_ids: set[str] = set()
        for emb in all_embeddings:
            retrieved = await retrieve_chunks(db, emb, request_body.filters, top_k=settings.top_k_retrieval)
            for chunk in retrieved:
                if chunk["id"] not in seen_ids:
                    seen_ids.add(chunk["id"])
                    all_chunks.append(chunk)

        # 5. Rerank
        reranked = rerank_chunks(clean_question, all_chunks, top_k=settings.top_k_rerank)

        # 6. Context assembly
        context_block = build_context_block(reranked)

        # Persist query record
        db_query = Query(
            id=uuid.UUID(query_id),
            session_id=request_body.session_id,
            user_id=user.id,
            question_raw=request_body.question,
            question_lang=language,
            filters_json=request_body.filters.model_dump() if request_body.filters else None,
            hitl_required=False,
            status="PROCESSING",
        )
        db.add(db_query)
        await db.commit()

        # 7 + 8. Stream answer from Claude
        full_answer = ""
        async for token in stream_answer(clean_question, context_block, language=language):
            full_answer += token
            yield f"data: {json.dumps({'delta': token, 'citations': [], 'hitl_required': False, 'query_id': query_id})}\n\n"

        # 9. Post-generation classification
        confidence = estimate_confidence(full_answer, reranked)
        citations = extract_citations(full_answer, reranked)
        risk_level, hitl_required = classify_risk(full_answer, confidence)

        latency_ms = int((time.perf_counter() - start_time) * 1000)

        # Persist response
        db_response = Response(
            id=uuid.uuid4(),
            query_id=db_query.id,
            answer_text=full_answer,
            citations_json=[c.model_dump() for c in citations],
            confidence_score=confidence,
            model_version=settings.llm_model,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=latency_ms,
        )
        db.add(db_response)
        await db.flush()

        # 10. HITL gate
        if hitl_required:
            await queue_for_review(db, db_response, db_query)
        else:
            db_query.status = "DELIVERED"
            await db.commit()

        # Token budget consumption (approximate)
        approx_tokens = len(full_answer.split()) + len(clean_question.split())
        await check_and_consume_tokens(str(user.id), approx_tokens)

        # Cache the result
        await _store_semantic_cache(
            redis,
            query_id,
            query_embedding,
            {"answer": full_answer, "citations": [c.model_dump() for c in citations]},
        )

        # Audit log
        await audit_log.log_event(
            db,
            event_type="QUERY_COMPLETED",
            actor_id=str(user.id),
            target_id=query_id,
            target_type="query",
            payload={"risk_level": risk_level, "hitl_required": hitl_required, "confidence": confidence},
            ip_address=ip,
        )

        # Final SSE event with full citations and HITL flag
        final = json.dumps(
            {
                "delta": "",
                "citations": [c.model_dump() for c in citations],
                "hitl_required": hitl_required,
                "query_id": query_id,
                "done": True,
            }
        )
        yield f"data: {final}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
