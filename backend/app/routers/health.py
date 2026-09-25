from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.middleware.auth import RequireEngineer, get_current_user, get_db
from app.middleware.rate_limit import get_redis
from app.models.db import AuditEvent, Chunk, Document, HITLReview, Query, Response, User
from app.models.schemas import HealthResponse
from app.services import insights_refresh
from app.tasks.celery_app import refresh_insights_batch_task

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health/live")
async def liveness() -> dict:
    """Process is up. No dependency checks — use for container liveness probes."""
    return {"status": "ok"}


@router.get("/health", response_model=HealthResponse)
async def health_check(response: FastAPIResponse, db: Annotated[AsyncSession, Depends(get_db)]) -> HealthResponse:
    """Readiness: 200 when Postgres and Redis answer, 503 otherwise. Error details go to logs only."""
    db_status = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Health check: database unreachable")
        db_status = "error"

    redis_status = "ok"
    try:
        await get_redis().ping()
    except Exception:
        logger.exception("Health check: redis unreachable")
        redis_status = "error"

    healthy = db_status == "ok" and redis_status == "ok"
    if not healthy:
        response.status_code = 503
    return HealthResponse(
        status="ok" if healthy else "degraded",
        database=db_status,
        redis=redis_status,
        version=settings.app_version,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
    )


@router.get("/health/stats")
async def health_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
) -> dict:
    total_users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    total_documents = (await db.execute(select(func.count()).select_from(Document))).scalar_one()
    total_queries = (await db.execute(select(func.count()).select_from(Query))).scalar_one()
    pending_reviews = (
        await db.execute(select(func.count()).select_from(HITLReview).where(HITLReview.decision.is_(None)))
    ).scalar_one()
    total_audit_events = (await db.execute(select(func.count()).select_from(AuditEvent))).scalar_one()
    avg_conf_raw = (await db.execute(select(func.avg(Response.confidence_score)))).scalar_one()
    avg_confidence_pct = round(float(avg_conf_raw) * 100, 1) if avg_conf_raw else 0.0

    return {
        "total_users": total_users,
        "total_documents": total_documents,
        "total_queries": total_queries,
        "pending_reviews": pending_reviews,
        "total_audit_events": total_audit_events,
        "avg_confidence_pct": avg_confidence_pct,
    }


_SEVERITY_ORDER = {"CRITICAL": 0, "OVERDUE": 1, "HIGH": 2, "DUE_SOON": 3, "MEDIUM": 4, "UPCOMING": 5, "LOW": 6}


@router.get("/dashboard/insights")
async def dashboard_insights(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
) -> dict:
    """
    Aggregate document intelligence: attention items, smart query suggestions,
    query trend (7 days), and confidence distribution.
    On first call, lazily extracts insights for up to 3 documents that lack them.
    """
    from app.services.insights import extract_document_insights

    # Lazily fill insights for docs that don't have them yet (up to 3 per call)
    missing_result = await db.execute(
        select(Document).where(Document.status == "COMPLETED", Document.insights_json.is_(None)).limit(3)
    )
    for doc in missing_result.scalars().all():
        chunks_result = await db.execute(
            select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.chunk_index).limit(8)
        )
        raw = [
            {"chunk_index": c.chunk_index, "text_content": c.text_content, "section_label": c.section_label}
            for c in chunks_result.scalars().all()
        ]
        doc.insights_json = await extract_document_insights(doc.filename, raw) or {}
    await db.commit()

    # Aggregate insights across all indexed documents
    docs_result = await db.execute(
        select(Document).where(Document.status == "COMPLETED", Document.insights_json.isnot(None))
    )
    docs = docs_result.scalars().all()

    attention_items: list[dict] = []
    suggested_queries: list[str] = []
    doc_summaries: list[dict] = []
    seen_queries: set[str] = set()

    for doc in docs:
        ins: dict = doc.insights_json or {}

        for d in ins.get("deadlines", []):
            attention_items.append(
                {
                    "type": "deadline",
                    "severity": d.get("status", "UPCOMING"),
                    "segment": d.get("segment"),
                    "item": d.get("item"),
                    "date": d.get("date"),
                    "days_until": d.get("days_until"),
                    "source": doc.filename,
                }
            )

        for a in ins.get("anomalies", []):
            attention_items.append(
                {
                    "type": "anomaly",
                    "severity": a.get("severity", "LOW"),
                    "segment": a.get("segment"),
                    "item": a.get("feature"),
                    "detail": a.get("detail"),
                    "action_required": a.get("action_required", False),
                    "source": doc.filename,
                }
            )

        for al in ins.get("alarms", []):
            attention_items.append(
                {
                    "type": "alarm",
                    "severity": "HIGH",
                    "segment": al.get("location"),
                    "item": f"{al.get('tag', 'Alarm')}",
                    "detail": al.get("description"),
                    "date": al.get("date"),
                    "source": doc.filename,
                }
            )

        for q in ins.get("suggested_queries", []):
            if q and q not in seen_queries:
                seen_queries.add(q)
                suggested_queries.append(q)

        if ins.get("summary"):
            doc_summaries.append(
                {
                    "filename": doc.filename,
                    "doc_type": ins.get("doc_type", "OTHER"),
                    "summary": ins.get("summary"),
                }
            )

    attention_items.sort(key=lambda x: _SEVERITY_ORDER.get(x.get("severity", "LOW"), 10))

    # 7-day query trend
    today_utc = datetime.utcnow().date()
    query_trend = []
    for i in range(6, -1, -1):
        day = today_utc - timedelta(days=i)
        count = (
            await db.execute(select(func.count()).select_from(Query).where(func.date(Query.query_ts) == day))
        ).scalar_one()
        query_trend.append({"date": day.isoformat(), "queries": count})

    # Confidence distribution
    all_conf = (await db.execute(select(Response.confidence_score))).scalars().all()
    conf_high = sum(1 for c in all_conf if c >= 0.75)
    conf_medium = sum(1 for c in all_conf if 0.40 <= c < 0.75)
    conf_low = sum(1 for c in all_conf if c < 0.40)

    return {
        "attention_items": attention_items[:20],
        "suggested_queries": suggested_queries[:6],
        "doc_summaries": doc_summaries,
        "query_trend": query_trend,
        "confidence_distribution": {"high": conf_high, "medium": conf_medium, "low": conf_low},
        "has_insights": len(doc_summaries) > 0,
    }


@router.post("/dashboard/insights/refresh", status_code=202)
async def refresh_insights(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(RequireEngineer)],
) -> dict:
    """Start re-analysing all completed documents in the background (except those ingested
    with summaries skipped). Returns progress; GET this path to follow it. A refresh that is
    already running is reported (already_running) rather than started twice."""
    return await insights_refresh.start(
        db, get_redis(), actor_id=str(current_user.id), dispatch=refresh_insights_batch_task.delay
    )


@router.get("/dashboard/insights/refresh")
async def refresh_insights_status(_: Annotated[User, Depends(get_current_user)]) -> dict:
    """Progress of the latest insights refresh: state is idle, running, done or interrupted."""
    return await insights_refresh.status(get_redis())
