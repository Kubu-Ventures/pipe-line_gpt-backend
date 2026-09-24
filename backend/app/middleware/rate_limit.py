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


async def record_token_usage(user_id: str, tokens_used: int) -> None:
    """Add actual usage to today's counter; enforcement happens in token_budget_dependency."""
    if tokens_used <= 0:
        return
    pipe = get_redis().pipeline()
    key = _day_key(user_id)
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


# ── Login brute-force protection ─────────────────────────────────────────────


def _login_keys(email: str, ip: str | None) -> list[tuple[str, int]]:
    """(redis key, failure limit) pairs. The IP limit is looser so shared NATs still work."""
    keys = [(f"login_fail:email:{email.lower()}", settings.login_max_failures)]
    if ip:
        keys.append((f"login_fail:ip:{ip}", settings.login_max_failures * 4))
    return keys


async def ensure_login_allowed(email: str, ip: str | None) -> None:
    """Raise 429 while the email or IP is locked out after repeated failures."""
    redis = get_redis()
    for key, limit in _login_keys(email, ip):
        count = await redis.get(key)
        if count and int(count) >= limit:
            ttl = await redis.ttl(key)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed sign-in attempts. Try again later.",
                headers={"Retry-After": str(max(ttl, 1))},
            )


async def record_login_failure(email: str, ip: str | None) -> None:
    redis = get_redis()
    pipe = redis.pipeline()
    for key, _ in _login_keys(email, ip):
        pipe.incr(key)
        pipe.expire(key, settings.login_lockout_seconds, nx=True)
    await pipe.execute()


async def clear_login_failures(email: str) -> None:
    await get_redis().delete(f"login_fail:email:{email.lower()}")


async def claim_totp_code(user_id: str, code: str) -> bool:
    """Single-use TOTP: returns False if this code was already accepted for the user (replay)."""
    return bool(await get_redis().set(f"totp_used:{user_id}:{code}", "1", ex=120, nx=True))
