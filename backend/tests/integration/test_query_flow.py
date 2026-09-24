"""End-to-end /query → HITL → /review flow against real Postgres/pgvector and Redis.

Embeddings and Claude are patched so the test is deterministic and offline.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.models.db import Chunk, Document, Query
from tests.integration.helpers import auth_headers

DIM = 384
UNIT_VEC = [1.0] + [0.0] * (DIM - 1)


@pytest.fixture
async def seeded_chunk(db_session):
    doc = Document(
        id=uuid.uuid4(),
        filename="ILI_Report.csv",
        source_type="csv",
        sha256_hash=uuid.uuid4().hex,
        status="COMPLETED",
        segment_id="SEG-TX-4B",
        chunk_count=1,
    )
    db_session.add(doc)
    db_session.add(
        Chunk(
            id=uuid.uuid4(),
            document_id=doc.id,
            chunk_index=0,
            text_content="SEG-TX-4B external corrosion 41.8% wall loss at odometer 2890.2 m.",
            token_count=12,
            embedding=UNIT_VEC,
        )
    )
    await db_session.commit()
    return doc


@pytest.fixture
def fake_llm(monkeypatch):
    """Patch the model-facing calls; set `fake_llm.answer` to control the streamed text."""

    class FakeLLM:
        answer = "There were 3 incidents on SEG-TX-4B [SRC-001]."
        fail = False

    async def embed_single(_text):
        return UNIT_VEC

    async def embed_texts(texts):
        return [UNIT_VEC for _ in texts]

    async def expand_query(_q):
        return []

    async def stream_answer(_q, _ctx, language="en"):
        if FakeLLM.fail:
            raise RuntimeError("upstream exploded")
        for token in FakeLLM.answer.split(" "):
            yield token + " "

    monkeypatch.setattr("app.routers.query.embed_single", embed_single)
    monkeypatch.setattr("app.routers.query.embed_texts", embed_texts)
    monkeypatch.setattr("app.routers.query.expand_query", expand_query)
    monkeypatch.setattr("app.routers.query.stream_answer", stream_answer)
    monkeypatch.setattr("app.routers.query.rerank_chunks", lambda _q, chunks, top_k=None: chunks[:top_k])
    return FakeLLM


def parse_sse(body: str) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in body.split("\n") if line.startswith("data: ")]


async def ask(client, user, question="How many incidents on SEG-TX-4B?"):
    resp = await client.post(
        "/query", json={"question": question, "session_id": "s1", "language": "en"}, headers=auth_headers(user)
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    return parse_sse(resp.text)


async def test_low_risk_answer_is_delivered(client, make_user, seeded_chunk, fake_llm, db_session):
    operator = await make_user("OPERATOR")
    events = await ask(client, operator)

    final = events[-1]
    assert final["done"] is True
    assert final["hitl_required"] is False
    assert [c["filename"] for c in final["citations"]] == ["ILI_Report.csv"]
    assert "".join(e["delta"] for e in events).strip() == fake_llm.answer

    q = await db_session.get(Query, uuid.UUID(final["query_id"]))
    assert q.status == "DELIVERED"

    history = (await client.get("/query/history", headers=auth_headers(operator))).json()
    assert len(history) == 1
    assert history[0]["status"] == "DELIVERED"


async def test_high_risk_answer_goes_through_review(client, make_user, seeded_chunk, fake_llm, db_session):
    fake_llm.answer = "Repair the anomaly at odometer 2890.2 m immediately [SRC-001]."
    operator = await make_user("OPERATOR")
    engineer = await make_user("ENGINEER", mfa_enabled=True)

    final = (await ask(client, operator))[-1]
    assert final["hitl_required"] is True
    query_id = final["query_id"]

    queue = (await client.get("/review?status=PENDING", headers=auth_headers(engineer))).json()
    assert [str(item["query_id"]) for item in queue] == [query_id]

    reject_without_reason = await client.post(
        f"/review/{query_id}", json={"decision": "REJECT"}, headers=auth_headers(engineer)
    )
    assert reject_without_reason.status_code == 422

    approve = await client.post(f"/review/{query_id}", json={"decision": "APPROVE"}, headers=auth_headers(engineer))
    assert approve.status_code == 200

    twice = await client.post(f"/review/{query_id}", json={"decision": "APPROVE"}, headers=auth_headers(engineer))
    assert twice.status_code == 409

    q = await db_session.get(Query, uuid.UUID(query_id), populate_existing=True)
    assert q.status == "DELIVERED"

    history = (await client.get("/query/history", headers=auth_headers(operator))).json()
    assert history[0]["decision"] == "APPROVE"
    assert history[0]["final_text"].strip() == fake_llm.answer


async def test_engineer_edit_replaces_answer(client, make_user, seeded_chunk, fake_llm):
    fake_llm.answer = "Shut in the line at MP 9.34 [SRC-001]."
    operator = await make_user("OPERATOR")
    engineer = await make_user("ENGINEER", mfa_enabled=True)
    query_id = (await ask(client, operator))[-1]["query_id"]

    edit = await client.post(
        f"/review/{query_id}",
        json={"decision": "EDIT", "final_text": "Schedule a field dig at MP 9.34."},
        headers=auth_headers(engineer),
    )
    assert edit.status_code == 200

    history = (await client.get("/query/history", headers=auth_headers(operator))).json()
    assert history[0]["final_text"] == "Schedule a field dig at MP 9.34."


async def test_semantic_cache_hit(client, make_user, seeded_chunk, fake_llm):
    operator = await make_user("OPERATOR")
    await ask(client, operator)
    second = await ask(client, operator)
    assert second[0].get("cached") is True


async def test_llm_failure_emits_error_and_done(client, make_user, seeded_chunk, fake_llm):
    fake_llm.fail = True
    operator = await make_user("OPERATOR")
    events = await ask(client, operator)
    assert events[0].get("error") is True
    assert events[-1]["done"] is True


async def test_query_writes_audit_event(client, make_user, seeded_chunk, fake_llm, db_session):
    from app.models.db import AuditEvent

    operator = await make_user("OPERATOR")
    await ask(client, operator)
    events = (
        (await db_session.execute(select(AuditEvent).where(AuditEvent.event_type == "QUERY_COMPLETED"))).scalars().all()
    )
    assert len(events) == 1
    assert events[0].actor_id == str(operator.id)
