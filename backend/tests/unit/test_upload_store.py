"""Staging directory and the worker task's handling of staged files."""

from __future__ import annotations

import uuid

import pytest

from app.services import upload_store
from app.tasks.celery_app import DocumentParseError, ingest_document_task


def test_save_read_remove():
    doc_id = str(uuid.uuid4())
    upload_store.save(doc_id, b"data")
    assert upload_store.read(doc_id) == b"data"
    assert not upload_store.path_for(doc_id).with_name(doc_id + ".partial").exists()
    upload_store.remove(doc_id)
    upload_store.remove(doc_id)  # already gone: no error
    assert not upload_store.path_for(doc_id).exists()


def test_save_from_copies_file(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4 content")
    doc_id = str(uuid.uuid4())
    upload_store.save_from(doc_id, source)
    assert upload_store.read(doc_id) == b"%PDF-1.4 content"
    assert source.exists()


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "not-a-uuid", ""])
def test_rejects_non_uuid_ids(bad_id):
    with pytest.raises(ValueError):
        upload_store.path_for(bad_id)


@pytest.fixture
def staged():
    doc_id = str(uuid.uuid4())
    upload_store.save(doc_id, b"a,b\n1,2\n")
    yield doc_id
    upload_store.remove(doc_id)


def _run_task(monkeypatch, doc_id, behaviour):
    calls = []

    async def fake_ingest(document_id, source_type, filename, content, **_options):
        calls.append((content, upload_store.path_for(document_id).exists()))
        return behaviour(len(calls))

    monkeypatch.setattr("app.tasks.celery_app._ingest_async", fake_ingest)
    ingest_document_task.apply(args=[doc_id, "csv", "x.csv"])
    return calls


def test_task_removes_staged_file_after_success(monkeypatch, staged):
    calls = _run_task(monkeypatch, staged, lambda _n: {"status": "COMPLETED"})
    assert calls == [(None, True)]  # content comes from staging, not the message
    assert not upload_store.path_for(staged).exists()


def test_task_removes_staged_file_after_parse_error(monkeypatch, staged):
    def fail(_n):
        raise DocumentParseError("bad file")

    calls = _run_task(monkeypatch, staged, fail)
    assert len(calls) == 1  # not retried
    assert not upload_store.path_for(staged).exists()


def test_task_keeps_staged_file_for_retries_then_removes_it(monkeypatch, staged):
    def fail(_n):
        raise ConnectionError("db down")

    calls = _run_task(monkeypatch, staged, fail)
    assert len(calls) == 1 + ingest_document_task.max_retries
    assert all(file_present for _content, file_present in calls)
    assert not upload_store.path_for(staged).exists()


def test_task_still_accepts_inline_content_from_older_messages(monkeypatch):
    received = []

    async def fake_ingest(document_id, source_type, filename, content, **_options):
        received.append(content)
        return {"status": "COMPLETED"}

    monkeypatch.setattr("app.tasks.celery_app._ingest_async", fake_ingest)
    ingest_document_task.apply(args=[str(uuid.uuid4()), "csv", "x.csv", "YSxiCg=="])
    assert received == [b"a,b\n"]
