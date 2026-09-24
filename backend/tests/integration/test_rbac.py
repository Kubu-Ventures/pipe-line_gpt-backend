"""Role matrix for read endpoints: who gets in, who gets 401/403."""

from __future__ import annotations

import pytest

from tests.integration.helpers import auth_headers

ROLES = ("OPERATOR", "ENGINEER", "ADMIN")

# path -> minimum role
ENDPOINTS = {
    "/query/history": "OPERATOR",
    "/ingest/history": "OPERATOR",
    "/health/stats": "OPERATOR",
    "/review": "ENGINEER",
    "/audit": "ENGINEER",
    "/audit/export": "ENGINEER",
    "/admin/users": "ADMIN",
    "/admin/invitations": "ADMIN",
}


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_unauthenticated_is_401(client, path):
    resp = await client.get(path)
    assert resp.status_code == 401


@pytest.mark.parametrize("path,min_role", ENDPOINTS.items())
@pytest.mark.parametrize("role", ROLES)
async def test_role_access(client, make_user, path, min_role, role):
    user = await make_user(role, mfa_enabled=True)
    resp = await client.get(path, headers=auth_headers(user))
    expected = 200 if ROLES.index(role) >= ROLES.index(min_role) else 403
    assert resp.status_code == expected, resp.text


async def test_operator_cannot_delete_documents(client, make_user):
    user = await make_user("OPERATOR")
    resp = await client.delete("/ingest/00000000-0000-0000-0000-000000000000", headers=auth_headers(user))
    assert resp.status_code == 403


async def test_admin_cannot_change_own_status(client, make_user):
    admin = await make_user("ADMIN", mfa_enabled=True)
    resp = await client.patch(
        f"/admin/users/{admin.id}/status", json={"status": "SUSPENDED"}, headers=auth_headers(admin)
    )
    assert resp.status_code == 400


async def test_admin_suspends_user(client, make_user):
    admin = await make_user("ADMIN", mfa_enabled=True)
    target = await make_user("OPERATOR")
    resp = await client.patch(
        f"/admin/users/{target.id}/status", json={"status": "SUSPENDED"}, headers=auth_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUSPENDED"


async def test_engineer_audit_hides_identity_events(client, make_user):
    from app.routers.audit import ADMIN_ONLY_EVENT_TYPES

    engineer = await make_user("ENGINEER", mfa_enabled=True)
    admin = await make_user("ADMIN", mfa_enabled=True)
    await client.post("/admin/invite", json={"email": "x@example.com", "role": "OPERATOR"}, headers=auth_headers(admin))

    admin_view = (await client.get("/audit", headers=auth_headers(admin))).json()
    engineer_view = (await client.get("/audit", headers=auth_headers(engineer))).json()

    assert any(e["event_type"] == "USER_INVITED" for e in admin_view["items"])
    assert not any(e["event_type"] in ADMIN_ONLY_EVENT_TYPES for e in engineer_view["items"])
