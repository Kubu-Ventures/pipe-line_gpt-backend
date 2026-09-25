"""Retry policy of the Celery ingest task."""

from __future__ import annotations

import uuid

from celery.exceptions import SoftTimeLimitExceeded

from app.services import upload_store
from app.tasks.celery_app import celery_app, ingest_document_task


def test_time_limit_is_not_retried(monkeypatch):
    calls = []

    async def too_slow(*args):
        calls.append(args)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr("app.tasks.celery_app._ingest_async", too_slow)
    doc_id = str(uuid.uuid4())
    upload_store.save(doc_id, b"%PDF-")
    result = ingest_document_task.apply(args=[doc_id, "pdf", "scan.pdf"]).get()

    assert len(calls) == 1
    assert result["status"] == "FAILED"
    assert not upload_store.path_for(doc_id).exists()  # not left behind in the uploads volume


def test_redis_redelivery_waits_longer_than_the_longest_task():
    visibility = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert visibility > celery_app.conf.task_time_limit
