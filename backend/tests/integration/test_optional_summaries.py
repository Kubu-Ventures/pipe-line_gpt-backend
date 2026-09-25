"""Documents ingested with summaries skipped stay out of Claude insight extraction."""

from __future__ import annotations

import uuid

from app.models.db import Document
from app.services.insights import SKIPPED
from app.tasks.celery_app import _ingest_async
from tests.integration.helpers import auth_headers

CSV = b"segment_id,inspection_date,feature_type\nSEG-TX-4B,2009-03-15,Dent\n"


async def fake_embed(texts):
    return [[0.1] * 384 for _ in texts]


def count_insight_calls(monkeypatch):
    calls: list[str] = []

    async def fake_insights(filename, _chunks):
        calls.append(filename)
        return {"doc_type": "OTHER", "summary": f"summary of {filename}"}

    monkeypatch.setattr("app.services.insights.extract_document_insights", fake_insights)
    return calls


async def add_doc(db_session, filename, insights, status="COMPLETED"):
    doc = Document(
        id=uuid.uuid4(), filename=filename, source_type="csv", sha256_hash=uuid.uuid4().hex * 2, status=status
    )
    # Left unset rather than None: SQLAlchemy stores an explicit None as JSON null, while the
    # dashboard looks for SQL NULL, which is what a document without a summary has.
    if insights is not None:
        doc.insights_json = insights
    db_session.add(doc)
    await db_session.commit()
    return doc.id


async def test_worker_skips_the_summary_when_asked(db_session, monkeypatch):
    monkeypatch.setattr("app.services.embedder.embed_texts", fake_embed)
    calls = count_insight_calls(monkeypatch)
    doc_id = await add_doc(db_session, "old.csv", None, status="PENDING")

    result = await _ingest_async(str(doc_id), "csv", "old.csv", CSV, summarize=False)

    assert result["status"] == "COMPLETED"
    assert calls == []
    doc = await db_session.get(Document, doc_id, populate_existing=True)
    assert doc.insights_json == SKIPPED


async def test_worker_summarizes_by_default(db_session, monkeypatch):
    monkeypatch.setattr("app.services.embedder.embed_texts", fake_embed)
    calls = count_insight_calls(monkeypatch)
    doc_id = await add_doc(db_session, "new.csv", None, status="PENDING")

    await _ingest_async(str(doc_id), "csv", "new.csv", CSV)

    assert calls == ["new.csv"]
    doc = await db_session.get(Document, doc_id, populate_existing=True)
    assert doc.insights_json["summary"] == "summary of new.csv"


async def test_dashboard_does_not_fill_in_skipped_summaries(client, make_user, db_session, monkeypatch):
    calls = count_insight_calls(monkeypatch)
    await add_doc(db_session, "skipped.csv", SKIPPED)
    await add_doc(db_session, "missing.csv", None)

    resp = await client.get("/dashboard/insights", headers=auth_headers(await make_user("OPERATOR")))

    assert resp.status_code == 200
    assert calls == ["missing.csv"]  # only the one that never had a summary
