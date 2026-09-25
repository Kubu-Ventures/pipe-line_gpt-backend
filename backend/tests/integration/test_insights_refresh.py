"""Dashboard insights refresh: started by the API, run by chained background batches."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select

from app.config import settings
from app.models.db import AuditEvent, Chunk, Document
from app.services import insights_refresh
from app.services.insights import SKIPPED
from tests.integration.helpers import auth_headers


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        "app.routers.health.refresh_insights_batch_task",
        SimpleNamespace(delay=lambda *args: calls.append(args) or SimpleNamespace(id="task")),
    )
    return calls


@pytest.fixture
async def redis():
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    yield client
    await client.aclose()


def fake_claude(monkeypatch, fail_for=()):
    calls: list[str] = []

    async def extract(filename, _chunks):
        calls.append(filename)
        return {} if filename in fail_for else {"summary": f"new summary of {filename}"}

    monkeypatch.setattr("app.services.insights.extract_document_insights", extract)
    return calls


async def add_doc(db_session, filename, insights=None, status="COMPLETED"):
    doc = Document(
        id=uuid.uuid4(), filename=filename, source_type="csv", sha256_hash=uuid.uuid4().hex * 2, status=status
    )
    if insights is not None:  # unset = SQL NULL, i.e. never summarised
        doc.insights_json = insights
    db_session.add(doc)
    db_session.add(Chunk(id=uuid.uuid4(), document_id=doc.id, chunk_index=0, text_content="text", token_count=1))
    await db_session.commit()
    return doc.id


async def test_post_queues_a_refresh_and_reports_progress(client, make_user, db_session, dispatched):
    engineer = await make_user("ENGINEER")
    await add_doc(db_session, "a.csv", {"summary": "old"})
    await add_doc(db_session, "b.csv")
    await add_doc(db_session, "skipped.csv", SKIPPED)
    await add_doc(db_session, "pending.csv", status="PENDING")

    resp = await client.post("/dashboard/insights/refresh", headers=auth_headers(engineer))

    assert resp.status_code == 202
    body = resp.json()
    assert (body["state"], body["total"], body["done"], body["already_running"]) == ("running", 2, 0, False)
    assert len(dispatched) == 1 and dispatched[0][1] is None

    again = await client.post("/dashboard/insights/refresh", headers=auth_headers(engineer))
    assert again.json()["already_running"] is True
    assert len(dispatched) == 1  # not started twice

    status = await client.get("/dashboard/insights/refresh", headers=auth_headers(await make_user("OPERATOR")))
    assert status.json()["state"] == "running"
    events = (await db_session.execute(select(AuditEvent.event_type))).scalars().all()
    assert events.count("INSIGHTS_REFRESH_STARTED") == 1


async def test_operators_cannot_start_a_refresh(client, make_user, dispatched):
    resp = await client.post("/dashboard/insights/refresh", headers=auth_headers(await make_user("OPERATOR")))
    assert resp.status_code == 403
    assert dispatched == []


async def test_idle_before_any_refresh(client, make_user):
    resp = await client.get("/dashboard/insights/refresh", headers=auth_headers(await make_user("OPERATOR")))
    assert resp.json()["state"] == "idle"


async def test_batches_refresh_every_eligible_document(client, make_user, db_session, dispatched, redis, monkeypatch):
    monkeypatch.setattr(insights_refresh, "BATCH_SIZE", 2)
    claude = fake_claude(monkeypatch, fail_for={"flaky.csv"})
    ids = {name: await add_doc(db_session, name, {"summary": "old"}) for name in ("a.csv", "b.csv", "flaky.csv")}
    ids["never.csv"] = await add_doc(db_session, "never.csv")
    ids["skipped.csv"] = await add_doc(db_session, "skipped.csv", SKIPPED)

    await client.post("/dashboard/insights/refresh", headers=auth_headers(await make_user("ENGINEER")))
    run_id, after = dispatched[0]
    batches = 0
    while True:  # what the chained Celery tasks do
        after = await insights_refresh.run_batch(db_session, redis, run_id, after)
        batches += 1
        if after is None:
            break

    assert batches == 3  # two full batches of 2, then an empty one that finishes the run
    assert sorted(claude) == ["a.csv", "b.csv", "flaky.csv", "never.csv"]
    summaries = {
        name: (await db_session.get(Document, doc_id, populate_existing=True)).insights_json
        for name, doc_id in ids.items()
    }
    assert summaries["a.csv"] == {"summary": "new summary of a.csv"}
    assert summaries["never.csv"] == {"summary": "new summary of never.csv"}
    assert summaries["flaky.csv"] == {"summary": "old"}  # a failed call keeps the previous insights
    assert summaries["skipped.csv"] == SKIPPED

    status = await insights_refresh.status(redis)
    assert (status["state"], status["total"], status["done"], status["failed"]) == ("done", 4, 3, 1)
    assert status["finished_at"] is not None
    assert await redis.get(insights_refresh.LOCK_KEY) is None  # a new refresh can start
    completed = (
        await db_session.execute(select(AuditEvent).where(AuditEvent.event_type == "INSIGHTS_REFRESH_COMPLETED"))
    ).scalar_one()
    assert completed.payload_json == {"run_id": run_id, "refreshed": 3, "failed": 1}


async def test_a_superseded_or_expired_run_stops(db_session, redis, monkeypatch):
    claude = fake_claude(monkeypatch)
    await add_doc(db_session, "a.csv", {"summary": "old"})
    await redis.set(insights_refresh.LOCK_KEY, "another-run")

    assert await insights_refresh.run_batch(db_session, redis, "old-run", None) is None
    assert claude == []


async def test_lost_worker_shows_as_interrupted(client, make_user, db_session, dispatched, redis):
    await add_doc(db_session, "a.csv", {"summary": "old"})
    await client.post("/dashboard/insights/refresh", headers=auth_headers(await make_user("ENGINEER")))
    await redis.delete(insights_refresh.LOCK_KEY)  # what LOCK_TTL_S does when no batch renews it

    assert (await insights_refresh.status(redis))["state"] == "interrupted"
