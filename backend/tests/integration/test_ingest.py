"""Upload API and the Celery ingest worker body, against real Postgres/pgvector."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models.db import AuditEvent, Chunk, Document
from app.services import upload_store
from tests.integration.helpers import auth_headers

CSV = (
    b"segment_id,inspection_date,feature_type,wall_loss_pct\n"
    b"SEG-TX-4B,2024-03-15,Metal Loss - External Corrosion,41.8\n"
    b"SEG-TX-7A,2023-11-02,Dent,0\n"
)


@pytest.fixture
def dispatched(monkeypatch):
    """Capture Celery dispatches instead of hitting the broker."""
    calls: list[tuple] = []

    def delay(*args):
        calls.append(args)
        return SimpleNamespace(id=f"task-{len(calls)}")

    monkeypatch.setattr("app.routers.ingest.ingest_document_task", SimpleNamespace(delay=delay))
    return calls


async def upload(client, user, content=CSV, filename="ili.csv", content_type="text/csv"):
    return await client.post("/ingest", files={"file": (filename, content, content_type)}, headers=auth_headers(user))


async def test_upload_creates_document_and_dispatches(client, make_user, dispatched, db_session):
    operator = await make_user("OPERATOR")
    resp = await upload(client, operator)
    assert resp.status_code == 202
    body = resp.json()
    assert body["task_id"] == "task-1"

    doc = await db_session.get(Document, uuid.UUID(body["document_id"]))
    assert doc.status == "PENDING"
    assert doc.source_type == "csv"
    assert doc.operator_id == operator.email

    # Only the ID travels through the queue; the file waits in the staging directory.
    assert dispatched[0] == (body["document_id"], "csv", "ili.csv")
    assert upload_store.read(body["document_id"]) == CSV

    audit = (await db_session.execute(select(AuditEvent).where(AuditEvent.event_type == "INGEST_SUBMITTED"))).scalars()
    assert len(audit.all()) == 1


async def test_duplicate_upload_is_skipped(client, make_user, dispatched):
    operator = await make_user("OPERATOR")
    first = await upload(client, operator)
    second = await upload(client, operator, filename="renamed.csv")
    assert second.json()["task_id"] == "dedup-skip"
    assert second.json()["document_id"] == first.json()["document_id"]
    assert len(dispatched) == 1


async def test_upload_too_large(client, make_user, dispatched, monkeypatch):
    monkeypatch.setattr("app.routers.ingest.settings.upload_max_bytes", 10)
    operator = await make_user("OPERATOR")
    resp = await upload(client, operator)
    assert resp.status_code == 413
    assert dispatched == []


async def test_upload_rejects_unsupported_mime(client, make_user, dispatched):
    operator = await make_user("OPERATOR")
    resp = await upload(
        client, operator, content=b"MZ...", filename="evil.exe", content_type="application/x-msdownload"
    )
    assert resp.status_code == 415


async def test_engineer_deletes_document_and_chunks(client, make_user, db_session):
    engineer = await make_user("ENGINEER", mfa_enabled=True)
    doc = Document(id=uuid.uuid4(), filename="a.csv", source_type="csv", sha256_hash="h" * 64, status="COMPLETED")
    db_session.add(doc)
    db_session.add(Chunk(document_id=doc.id, chunk_index=0, text_content="x", embedding=[0.0] * 384))
    await db_session.commit()

    resp = await client.delete(f"/ingest/{doc.id}", headers=auth_headers(engineer))
    assert resp.status_code == 200

    remaining = await db_session.execute(select(func.count()).select_from(Chunk))
    assert remaining.scalar_one() == 0


async def test_worker_ingests_csv(make_user, db_session, monkeypatch):
    """Run the Celery task body directly (no broker) with a fake embedder."""
    from app.tasks.celery_app import _ingest_async

    async def fake_embed(texts):
        return [[0.1] * 384 for _ in texts]

    async def fake_insights(_filename, _chunks):
        return {"doc_type": "ILI_REPORT"}

    monkeypatch.setattr("app.services.embedder.embed_texts", fake_embed)
    monkeypatch.setattr("app.services.insights.extract_document_insights", fake_insights)

    doc = Document(id=uuid.uuid4(), filename="ili.csv", source_type="csv", sha256_hash="c" * 64, status="PENDING")
    db_session.add(doc)
    await db_session.commit()

    doc_id = doc.id
    result = await _ingest_async(str(doc_id), "csv", "ili.csv", CSV)
    assert result["status"] == "COMPLETED"

    doc = await db_session.get(Document, doc_id, populate_existing=True)
    assert doc.status == "COMPLETED"
    assert doc.chunk_count == result["chunk_count"] > 0
    assert doc.insights_json == {"doc_type": "ILI_REPORT"}


# ── Hardening ────────────────────────────────────────────────────────────────


async def test_status_requires_auth(client):
    assert (await client.get("/ingest/status/some-task")).status_code == 401


async def test_unknown_extension_rejected(client, make_user, dispatched):
    operator = await make_user("OPERATOR")
    resp = await upload(
        client, operator, content=b"hello", filename="notes.docx", content_type="application/octet-stream"
    )
    assert resp.status_code == 415
    assert dispatched == []


async def test_fake_pdf_rejected(client, make_user, dispatched):
    operator = await make_user("OPERATOR")
    resp = await upload(client, operator, content=b"not a pdf", filename="report.pdf", content_type="application/pdf")
    assert resp.status_code == 415


async def test_empty_upload_rejected(client, make_user, dispatched):
    operator = await make_user("OPERATOR")
    assert (await upload(client, operator, content=b"")).status_code == 400


async def test_demo_sync_disabled_outside_demo_mode(client, make_user):
    operator = await make_user("OPERATOR")
    assert (await client.post("/ingest/phmsa-sync", headers=auth_headers(operator))).status_code == 404


async def test_worker_retry_does_not_duplicate_chunks(db_session, monkeypatch):
    from app.tasks.celery_app import _ingest_async

    async def fake_embed(texts):
        return [[0.1] * 384 for _ in texts]

    monkeypatch.setattr("app.services.embedder.embed_texts", fake_embed)
    monkeypatch.setattr("app.services.insights.extract_document_insights", _no_insights)

    doc = Document(id=uuid.uuid4(), filename="ili.csv", source_type="csv", sha256_hash="d" * 64, status="PENDING")
    db_session.add(doc)
    await db_session.commit()

    first = await _ingest_async(str(doc.id), "csv", "ili.csv", CSV)
    await _ingest_async(str(doc.id), "csv", "ili.csv", CSV)  # e.g. redelivered after worker loss

    count = (await db_session.execute(select(func.count()).select_from(Chunk))).scalar_one()
    assert count == first["chunk_count"]


async def test_worker_parse_failure_marks_failed(db_session, monkeypatch):
    from app.tasks.celery_app import DocumentParseError, _ingest_async

    doc = Document(id=uuid.uuid4(), filename="bad.pdf", source_type="pdf", sha256_hash="e" * 64, status="PENDING")
    doc_id = doc.id
    db_session.add(doc)
    await db_session.commit()

    with pytest.raises(DocumentParseError):
        await _ingest_async(str(doc_id), "pdf", "bad.pdf", b"%PDF-garbage")

    doc = await db_session.get(Document, doc_id, populate_existing=True)
    assert doc.status == "FAILED"
    audit = (await db_session.execute(select(AuditEvent.event_type))).scalars().all()
    assert "INGEST_FAILED" in audit


async def test_worker_runs_in_separate_event_loops(db_session, monkeypatch):
    """Celery calls asyncio.run per task; a second task must not reuse the first loop's connections."""
    import asyncio
    import threading

    from app.tasks.celery_app import _ingest_async

    async def fake_embed(texts):
        return [[0.1] * 384 for _ in texts]

    monkeypatch.setattr("app.services.embedder.embed_texts", fake_embed)
    monkeypatch.setattr("app.services.insights.extract_document_insights", _no_insights)

    ids = []
    for letter in "fg":
        doc = Document(
            id=uuid.uuid4(), filename=f"{letter}.csv", source_type="csv", sha256_hash=letter * 64, status="PENDING"
        )
        db_session.add(doc)
        ids.append(str(doc.id))
    await db_session.commit()

    results: list[dict] = []

    def run_like_celery():
        for doc_id in ids:
            results.append(asyncio.run(_ingest_async(doc_id, "csv", "x.csv", CSV)))

    thread = threading.Thread(target=run_like_celery)
    thread.start()
    await asyncio.to_thread(thread.join)
    assert [r["status"] for r in results] == ["COMPLETED", "COMPLETED"]


async def _no_insights(_filename, _chunks):
    return {}


async def test_worker_reads_the_staged_file(db_session, monkeypatch):
    from app.tasks.celery_app import _ingest_async

    async def fake_embed(texts):
        return [[0.1] * 384 for _ in texts]

    monkeypatch.setattr("app.services.embedder.embed_texts", fake_embed)
    monkeypatch.setattr("app.services.insights.extract_document_insights", _no_insights)

    doc = Document(id=uuid.uuid4(), filename="ili.csv", source_type="csv", sha256_hash="h" * 64, status="PENDING")
    db_session.add(doc)
    await db_session.commit()
    upload_store.save(str(doc.id), CSV)

    result = await _ingest_async(str(doc.id), "csv", "ili.csv")
    assert result["status"] == "COMPLETED"
    assert result["chunk_count"] > 0


async def test_worker_fails_cleanly_when_staged_file_is_missing(db_session):
    """E.g. UPLOAD_DIR not shared between API and worker: the document must not stay PENDING."""
    from app.tasks.celery_app import DocumentParseError, _ingest_async

    doc = Document(id=uuid.uuid4(), filename="gone.csv", source_type="csv", sha256_hash="i" * 64, status="PENDING")
    doc_id = doc.id
    db_session.add(doc)
    await db_session.commit()

    with pytest.raises(DocumentParseError, match="missing from the staging directory"):
        await _ingest_async(str(doc_id), "csv", "gone.csv")

    doc = await db_session.get(Document, doc_id, populate_existing=True)
    assert doc.status == "FAILED"
