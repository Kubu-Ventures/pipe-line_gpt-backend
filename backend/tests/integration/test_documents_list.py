"""GET /ingest/documents: paging, search and knowledge-base totals."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.db import Document
from tests.integration.helpers import auth_headers

T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def add_docs(db_session, specs, start=0):
    """specs: (filename, status, chunk_count), oldest first, from start minutes after T0."""
    for i, (filename, doc_status, chunks) in enumerate(specs):
        db_session.add(
            Document(
                id=uuid.uuid4(),
                filename=filename,
                source_type="pdf",
                sha256_hash=uuid.uuid4().hex * 2,
                status=doc_status,
                chunk_count=chunks,
                operator_id="bulk-import",
                ingest_date=T0 + timedelta(minutes=start + i),
            )
        )
    await db_session.commit()


async def get(client, user, **params):
    resp = await client.get("/ingest/documents", params=params, headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_pages_newest_first_with_whole_base_totals(client, make_user, db_session):
    operator = await make_user("OPERATOR")
    await add_docs(db_session, [(f"1998/ILI/run-{i:03}.pdf", "COMPLETED", 10) for i in range(120)])
    await add_docs(db_session, [("2024/new.pdf", "PENDING", 0), ("2024/bad.pdf", "FAILED", 0)], start=120)

    first = await get(client, operator, limit=50)
    assert first["total"] == 122
    assert [d["filename"] for d in first["items"][:3]] == ["2024/bad.pdf", "2024/new.pdf", "1998/ILI/run-119.pdf"]
    assert first["items"][0]["uploaded_by"] == "bulk-import"
    assert first["summary"] == {
        "documents": 122,
        "by_status": {"PENDING": 1, "PROCESSING": 0, "COMPLETED": 120, "FAILED": 1},
        "total_chunks": 1200,
    }

    last = await get(client, operator, limit=50, offset=100)
    assert len(last["items"]) == 22
    assert last["items"][-1]["filename"] == "1998/ILI/run-000.pdf"

    seen = set()
    for offset in range(0, 122, 50):
        seen.update(d["id"] for d in (await get(client, operator, limit=50, offset=offset))["items"])
    assert len(seen) == 122  # stable order: no document skipped or repeated across pages


async def test_search_and_status_filter_leave_summary_whole(client, make_user, db_session):
    operator = await make_user("OPERATOR")
    await add_docs(
        db_session,
        [
            ("1998/ILI/segment-14.pdf", "COMPLETED", 5),
            ("2009/ILI/segment-14.pdf", "FAILED", 0),
            ("2009/CP/survey.pdf", "COMPLETED", 3),
            ("100%_complete.pdf", "COMPLETED", 1),
        ],
    )

    page = await get(client, operator, q="ili/SEGMENT")
    assert sorted(d["filename"] for d in page["items"]) == ["1998/ILI/segment-14.pdf", "2009/ILI/segment-14.pdf"]
    assert page["total"] == 2
    assert page["summary"]["documents"] == 4

    page = await get(client, operator, q="2009", status="COMPLETED")
    assert [d["filename"] for d in page["items"]] == ["2009/CP/survey.pdf"]

    # LIKE wildcards in the search text are matched literally.
    assert [d["filename"] for d in (await get(client, operator, q="%_"))["items"]] == ["100%_complete.pdf"]
    assert (await get(client, operator, q="_"))["total"] == 1


async def test_empty_knowledge_base(client, make_user):
    page = await get(client, await make_user("OPERATOR"))
    assert page["items"] == []
    assert page["total"] == 0
    assert page["summary"]["by_status"] == {"PENDING": 0, "PROCESSING": 0, "COMPLETED": 0, "FAILED": 0}


async def test_rejects_bad_parameters_and_anonymous_callers(client, make_user):
    operator = await make_user("OPERATOR")
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"status": "DONE"}):
        resp = await client.get("/ingest/documents", params=params, headers=auth_headers(operator))
        assert resp.status_code == 422, params
    assert (await client.get("/ingest/documents")).status_code == 401
