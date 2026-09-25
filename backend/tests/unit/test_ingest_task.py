"""Retry policy of the Celery ingest task."""

from __future__ import annotations

import uuid

from celery.exceptions import SoftTimeLimitExceeded

from app.tasks.celery_app import celery_app, ingest_document_task


def test_time_limit_is_not_retried(monkeypatch):
    calls = []

    async def too_slow(*args):
        calls.append(args)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr("app.tasks.celery_app._ingest_async", too_slow)
    result = ingest_document_task.apply(args=[str(uuid.uuid4()), "pdf", "scan.pdf", "JVBERi0="]).get()

    assert len(calls) == 1
    assert result["status"] == "FAILED"


def test_redis_redelivery_waits_longer_than_the_longest_task():
    visibility = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert visibility > celery_app.conf.task_time_limit
