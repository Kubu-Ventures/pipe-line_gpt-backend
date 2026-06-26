from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import get_db
from app.middleware.rate_limit import get_redis
from app.models.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    # Check DB
    db_status = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {e}"

    # Check Redis
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
