from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import get_current_user, get_db
from app.middleware.rate_limit import get_redis
from app.models.db import AuditEvent, Chunk, Document, HITLReview, Query, Response, User
from app.models.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    db_status = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {e}"

    redis_status = "ok"
    try:
        redis = get_redis()
        await redis.ping()
    except Exception as e:
        redis_status = f"error: {e}"

    return HealthResponse(
        status="ok" if db_status == "ok" and redis_status == "ok" else "degraded",
        database=db_status,
        redis=redis_status,
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


@router.post("/dashboard/insights/refresh")
async def refresh_insights(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Re-run insight extraction for ALL completed documents. Engineer/Admin only."""
    if current_user.role not in ("ENGINEER", "ADMIN"):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Engineer or Admin required")

    from app.services.insights import extract_document_insights

    docs_result = await db.execute(select(Document).where(Document.status == "COMPLETED"))
    docs = docs_result.scalars().all()
    refreshed = 0
    for doc in docs:
        chunks_result = await db.execute(
            select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.chunk_index).limit(8)
        )
        raw = [
            {"chunk_index": c.chunk_index, "text_content": c.text_content, "section_label": c.section_label}
            for c in chunks_result.scalars().all()
        ]
        doc.insights_json = await extract_document_insights(doc.filename, raw) or {}
        refreshed += 1
    await db.commit()
    return {"refreshed": refreshed}
