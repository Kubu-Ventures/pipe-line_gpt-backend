"""Bulk import of an archive folder, against real Postgres."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app.models.db import AuditEvent, Document
from app.services import upload_store
from app.services.bulk_import import import_folder

CSV = b"segment_id,inspection_date,feature_type\nSEG-TX-4B,2009-03-15,Dent\n"


def make_archive(root):
    (root / "2009" / "ILI").mkdir(parents=True)
    (root / "2014").mkdir()
    (root / ".git").mkdir()
    (root / "2009" / "ILI" / "report.pdf").write_bytes(b"%PDF-1.4 inspection report")
    (root / "2014" / "report.pdf").write_bytes(b"%PDF-1.4 a different report")
    (root / "2009" / "anomalies.csv").write_bytes(CSV)
    (root / "2014" / "anomalies-copy.csv").write_bytes(CSV)  # same content, other name
    (root / "2014" / "fake.pdf").write_bytes(b"MZ not really a pdf")
    (root / "2014" / "memo.docx").write_bytes(b"PK word file")
    (root / "2014" / "empty.csv").write_bytes(b"")
    (root / "2014" / "huge.csv").write_bytes(b"x" * 2048)
    (root / ".git" / "config.txt").write_bytes(b"hidden")


def dispatcher():
    calls: list[tuple] = []

    def delay(*args):
        calls.append(args)
        return SimpleNamespace(id=f"task-{len(calls)}")

    return delay, calls


async def run(db_session, root, **kwargs):
    delay, calls = dispatcher()
    reported: dict[str, str] = {}
    counts = await import_folder(
        db_session,
        root,
        dispatch=delay,
        operator_id="bulk-import",
        max_bytes=1024,
        report=lambda name, outcome, _detail: reported.__setitem__(name, outcome),
        **kwargs,
    )
    return counts, calls, reported


async def test_queues_supported_files_and_skips_the_rest(db_session, tmp_path):
    make_archive(tmp_path)
    counts, calls, reported = await run(db_session, tmp_path)

    assert reported == {
        "2009/ILI/report.pdf": "queued",
        "2009/anomalies.csv": "queued",
        "2014/anomalies-copy.csv": "duplicate",
        "2014/empty.csv": "skipped",
        "2014/fake.pdf": "skipped",
        "2014/huge.csv": "skipped",
        "2014/memo.docx": "skipped",
        "2014/report.pdf": "queued",
    }
    assert dict(counts) == {"queued": 3, "duplicate": 1, "skipped": 4}

    docs = (await db_session.execute(select(Document))).scalars().all()
    assert sorted(d.filename for d in docs) == ["2009/ILI/report.pdf", "2009/anomalies.csv", "2014/report.pdf"]
    assert {d.status for d in docs} == {"PENDING"}
    assert {d.operator_id for d in docs} == {"bulk-import"}

    by_name = {d.filename: d for d in docs}
    assert (str(by_name["2009/anomalies.csv"].id), "csv", "2009/anomalies.csv") in calls
    assert upload_store.read(str(by_name["2009/ILI/report.pdf"].id)) == b"%PDF-1.4 inspection report"

    audit = (await db_session.execute(select(AuditEvent).where(AuditEvent.event_type == "INGEST_SUBMITTED"))).scalars()
    assert [e.payload_json["via"] for e in audit.all()] == ["bulk_import"] * 3


async def test_rerun_after_interruption_queues_nothing_twice(db_session, tmp_path):
    make_archive(tmp_path)
    await run(db_session, tmp_path)
    (tmp_path / "2020.csv").write_bytes(b"segment_id\nSEG-NEW\n")  # added since the first run

    counts, calls, _ = await run(db_session, tmp_path)
    assert counts["queued"] == 1
    assert [c[2] for c in calls] == ["2020.csv"]


async def test_dry_run_changes_nothing(db_session, tmp_path):
    make_archive(tmp_path)
    counts, calls, _ = await run(db_session, tmp_path, dry_run=True)

    assert counts["would queue"] == 3
    assert calls == []
    assert (await db_session.execute(select(Document))).first() is None


async def test_file_already_uploaded_through_the_ui_is_a_duplicate(db_session, tmp_path):
    from app.services.embedder import content_hash

    db_session.add(
        Document(
            id=uuid.uuid4(), filename="ui.csv", source_type="csv", sha256_hash=content_hash(CSV), status="COMPLETED"
        )
    )
    await db_session.commit()
    (tmp_path / "anomalies.csv").write_bytes(CSV)

    counts, calls, _ = await run(db_session, tmp_path)
    assert dict(counts) == {"duplicate": 1}
    assert calls == []
