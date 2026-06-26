from __future__ import annotations

import time

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, status

from app.config import settings
from app.middleware.auth import get_current_user
from app.models.db import User

_redis_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _day_key(user_id: str) -> str:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return f"token_budget:{user_id}:{day}"


async def check_and_consume_tokens(user_id: str, tokens_used: int) -> None:
    """Raise 429 if the user's daily token budget is exhausted."""
    redis = get_redis()
    key = _day_key(user_id)
    current = await redis.get(key)
    used = int(current) if current else 0

    if used + tokens_used > settings.max_tokens_per_day:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily token budget of {settings.max_tokens_per_day} tokens exceeded.",
        )

    pipe = redis.pipeline()
    pipe.incrby(key, tokens_used)
    pipe.expire(key, 86_400)
    await pipe.execute()


async def token_budget_dependency(user: User = Depends(get_current_user)) -> None:
    """FastAPI dependency — checks budget before route logic runs (pre-check with 0 tokens)."""
    redis = get_redis()
    key = _day_key(str(user.id))
    current = await redis.get(key)
    used = int(current) if current else 0

    if used >= settings.max_tokens_per_day:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily token budget exhausted. Try again tomorrow.",
        )
