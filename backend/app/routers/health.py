from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import get_current_user, get_db
from app.middleware.rate_limit import get_redis
from app.models.db import AuditEvent, Document, HITLReview, Query, Response, User
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
    pending_reviews = (await db.execute(
        select(func.count()).select_from(HITLReview).where(HITLReview.decision.is_(None))
    )).scalar_one()
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
