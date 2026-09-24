"""Redis semantic cache for delivered (non-HITL) answers.

Entries are partitioned by a scope hash of (language, filters) so an answer is only
reused for an equivalent request, and the whole cache is dropped whenever the
document corpus changes.
"""

from __future__ import annotations

import hashlib
import json
import logging

import redis.asyncio as aioredis

from app.config import settings
from app.services.embedder import cosine_similarity

logger = logging.getLogger(__name__)

PREFIX = "query_cache"
# Upper bound on entries compared per lookup, so a large cache can't stall a request.
MAX_SCAN = 2_000


def cache_scope(language: str, filters: dict | None) -> str:
    raw = json.dumps({"lang": (language or "en").lower(), "filters": filters or {}}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def lookup(redis: aioredis.Redis, scope: str, embedding: list[float]) -> dict | None:
    try:
        keys: list[str] = []
        async for key in redis.scan_iter(match=f"{PREFIX}:{scope}:*", count=500):
            keys.append(key)
            if len(keys) >= MAX_SCAN:
                break
        best: tuple[float, dict] | None = None
        for start in range(0, len(keys), 200):
            for raw in await redis.mget(keys[start : start + 200]):
                if not raw:
                    continue
                entry = json.loads(raw)
                sim = cosine_similarity(embedding, entry.get("embedding") or [])
                if sim >= settings.semantic_cache_similarity and (best is None or sim > best[0]):
                    best = (sim, entry)
        return best[1] if best else None
    except Exception:
        logger.warning("Semantic cache lookup failed", exc_info=True)
        return None


async def store(redis: aioredis.Redis, scope: str, query_id: str, embedding: list[float], data: dict) -> None:
    try:
        payload = json.dumps({"embedding": embedding, **data})
        await redis.setex(f"{PREFIX}:{scope}:{query_id}", settings.semantic_cache_ttl_seconds, payload)
    except Exception:
        logger.warning("Semantic cache write failed", exc_info=True)


async def invalidate(redis: aioredis.Redis) -> None:
    """Drop every cached answer (call after documents are added or removed)."""
    try:
        batch: list[str] = []
        async for key in redis.scan_iter(match=f"{PREFIX}:*", count=1000):
            batch.append(key)
            if len(batch) >= 1000:
                await redis.delete(*batch)
                batch.clear()
        if batch:
            await redis.delete(*batch)
    except Exception:
        logger.warning("Semantic cache invalidation failed", exc_info=True)
