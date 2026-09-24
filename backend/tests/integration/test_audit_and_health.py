from __future__ import annotations

import csv
import io
import uuid

from app.models.db import AuditEvent
from tests.integration.helpers import auth_headers


async def test_legacy_hitl_event_names_are_normalized(client, make_user, db_session):
    admin = await make_user("ADMIN", mfa_enabled=True)
    db_session.add(AuditEvent(id=uuid.uuid4(), event_type="HITL_APPROVE", actor_id=str(admin.id)))
    await db_session.commit()

    listed = (await client.get("/audit?event_type=HITL_APPROVED", headers=auth_headers(admin))).json()
    assert [e["event_type"] for e in listed["items"]] == ["HITL_APPROVED"]


async def test_export_neutralizes_formula_injection(client, make_user, db_session):
    admin = await make_user("ADMIN", mfa_enabled=True)
    db_session.add(
        AuditEvent(
            id=uuid.uuid4(),
            event_type="INGEST_SUBMITTED",
            actor_id=str(admin.id),
            payload_json={"filename": '=HYPERLINK("http://evil","click")'},
        )
    )
    await db_session.commit()

    resp = await client.get("/audit/export", headers=auth_headers(admin))
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    cells = [cell for row in rows[1:] for cell in row]
    assert not any(cell.startswith("=") for cell in cells)
    assert any(cell.startswith("'=HYPERLINK") for cell in cells)


async def test_health_ready_and_live(client):
    ready = await client.get("/health")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
    assert (await client.get("/health/live")).json() == {"status": "ok"}


async def test_health_reports_503_without_leaking_errors(client, monkeypatch):
    class BrokenRedis:
        async def ping(self):
            raise ConnectionError("redis://secret-host:6379 refused")

    monkeypatch.setattr("app.routers.health.get_redis", lambda: BrokenRedis())
    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["redis"] == "error"
    assert "secret-host" not in resp.text


async def test_security_headers_present(client):
    resp = await client.get("/health/live")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
