"""Re-analyse every summarised document with Claude, in the background.

A refresh is a chain of short Celery tasks of BATCH_SIZE documents each, so it never
approaches the task time limit or the Redis redelivery window, and uploads queued in
the meantime run between batches instead of waiting hours. Only one refresh runs at a
time: a Redis lock holds its run id and is renewed by every batch, so a refresh whose
worker died frees itself after LOCK_TTL_S. Progress lives in a Redis hash for the
dashboard to poll.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Chunk, Document
from app.services import audit_log, insights

BATCH_SIZE = 25
LOCK_TTL_S = 3600
PROGRESS_TTL_S = 86_400
LOCK_KEY = "insights_refresh:run"
PROGRESS_KEY = "insights_refresh:progress"


def _eligible():
    """Completed documents, except those ingested with summaries skipped."""
    return (
        Document.status == "COMPLETED",
        or_(Document.insights_json.is_(None), Document.insights_json["skipped"].as_boolean().is_not(True)),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def status(redis: aioredis.Redis) -> dict:
    """state: idle (never run or expired), running, done, or interrupted (worker lost)."""
    progress = await redis.hgetall(PROGRESS_KEY)
    if not progress:
        return {"state": "idle", "total": 0, "done": 0, "failed": 0, "started_at": None, "finished_at": None}
    running = await redis.get(LOCK_KEY) == progress["run_id"]
    state = progress["state"]
    if state == "running" and not running:
        state = "interrupted"
    total = int(progress["total"])
    return {
        "state": state,
        "total": total,
        # A retried batch can re-count a few documents.
        "done": min(int(progress["done"]), total),
        "failed": int(progress["failed"]),
        "started_at": progress["started_at"],
        "finished_at": progress.get("finished_at") or None,
    }


async def start(
    db: AsyncSession, redis: aioredis.Redis, *, actor_id: str, dispatch: Callable[[str, str | None], object]
) -> dict:
    """Start a refresh, or report the one already running. dispatch(run_id, after_id)
    queues the first batch."""
    run_id = uuid.uuid4().hex
    if not await redis.set(LOCK_KEY, run_id, nx=True, ex=LOCK_TTL_S):
        return {**await status(redis), "already_running": True}

    total = (await db.execute(select(func.count()).select_from(Document).where(*_eligible()))).scalar_one()
    await redis.delete(PROGRESS_KEY)
    await redis.hset(
        PROGRESS_KEY,
        mapping={"run_id": run_id, "state": "running", "total": total, "done": 0, "failed": 0, "started_at": _now()},
    )
    await redis.expire(PROGRESS_KEY, PROGRESS_TTL_S)
    dispatch(run_id, None)
    await audit_log.log_event(
        db, event_type="INSIGHTS_REFRESH_STARTED", actor_id=actor_id, payload={"run_id": run_id, "documents": total}
    )
    return {**await status(redis), "already_running": False}


async def run_batch(db: AsyncSession, redis: aioredis.Redis, run_id: str, after_id: str | None) -> str | None:
    """Refresh the next BATCH_SIZE documents after after_id. Returns the cursor for the
    next batch, or None when the refresh is finished (or no longer the current one)."""
    if await redis.get(LOCK_KEY) != run_id:
        return None  # superseded, or its lock expired
    await redis.expire(LOCK_KEY, LOCK_TTL_S)

    stmt = select(Document).where(*_eligible()).order_by(Document.id).limit(BATCH_SIZE)
    if after_id is not None:
        stmt = stmt.where(Document.id > uuid.UUID(after_id))
    docs = (await db.execute(stmt)).scalars().all()

    for doc in docs:
        chunks = (
            await db.execute(select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.chunk_index).limit(8))
        ).scalars()
        raw = [
            {"chunk_index": c.chunk_index, "text_content": c.text_content, "section_label": c.section_label}
            for c in chunks
        ]
        fresh = await insights.extract_document_insights(doc.filename, raw)
        if fresh:
            doc.insights_json = fresh
            await db.commit()
            await redis.hincrby(PROGRESS_KEY, "done", 1)
        else:
            # Claude failed or returned nothing usable: keep the previous insights.
            await redis.hincrby(PROGRESS_KEY, "failed", 1)

    if len(docs) == BATCH_SIZE:
        return str(docs[-1].id)

    await redis.hset(PROGRESS_KEY, mapping={"state": "done", "finished_at": _now()})
    if await redis.get(LOCK_KEY) == run_id:
        await redis.delete(LOCK_KEY)
    final = await status(redis)
    await audit_log.log_event(
        db,
        event_type="INSIGHTS_REFRESH_COMPLETED",
        payload={"run_id": run_id, "refreshed": final["done"], "failed": final["failed"]},
    )
    return None
