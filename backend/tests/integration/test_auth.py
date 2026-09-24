from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
from sqlalchemy import select

from app.models.db import AuditEvent, Invitation, User
from tests.integration.helpers import DEFAULT_PASSWORD, auth_headers


async def test_login_returns_token(client, make_user):
    user = await make_user("OPERATOR")
    resp = await client.post("/auth/login", json={"email": user.email, "password": DEFAULT_PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["mfa_setup_required"] is False

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == user.email


async def test_login_flags_mfa_setup_for_engineer_without_mfa(client, make_user):
    user = await make_user("ENGINEER", mfa_enabled=False)
    resp = await client.post("/auth/login", json={"email": user.email, "password": DEFAULT_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["mfa_setup_required"] is True


async def test_login_wrong_password(client, make_user):
    user = await make_user()
    resp = await client.post("/auth/login", json={"email": user.email, "password": "wrong-password"})
    assert resp.status_code == 401


async def test_login_unknown_email(client):
    resp = await client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever1"})
    assert resp.status_code == 401


async def test_login_suspended_user_forbidden(client, make_user):
    user = await make_user(status="SUSPENDED")
    resp = await client.post("/auth/login", json={"email": user.email, "password": DEFAULT_PASSWORD})
    assert resp.status_code == 403


async def test_login_writes_audit_event(client, make_user, db_session):
    user = await make_user()
    await client.post("/auth/login", json={"email": user.email, "password": DEFAULT_PASSWORD})
    events = (await db_session.execute(select(AuditEvent).where(AuditEvent.event_type == "USER_LOGIN"))).scalars().all()
    assert [e.actor_id for e in events] == [str(user.id)]


async def test_me_rejects_garbage_token(client):
    resp = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


async def test_invite_accept_login_flow(client, make_user):
    admin = await make_user("ADMIN", mfa_enabled=True)

    invite = await client.post(
        "/admin/invite", json={"email": "New.Hire@Example.com", "role": "OPERATOR"}, headers=auth_headers(admin)
    )
    assert invite.status_code == 201
    token = invite.json()["token"]

    dup = await client.post(
        "/admin/invite", json={"email": "new.hire@example.com", "role": "OPERATOR"}, headers=auth_headers(admin)
    )
    assert dup.status_code == 409

    accept = await client.post("/auth/accept-invite", json={"token": token, "password": "S3cure-pass"})
    assert accept.status_code == 201

    again = await client.post("/auth/accept-invite", json={"token": token, "password": "S3cure-pass"})
    assert again.status_code == 409

    login = await client.post("/auth/login", json={"email": "new.hire@example.com", "password": "S3cure-pass"})
    assert login.status_code == 200


async def test_accept_expired_invite(client, make_user, db_session):
    admin = await make_user("ADMIN", mfa_enabled=True)
    invite = Invitation(
        email="late@example.com",
        role="OPERATOR",
        token="expired-token",
        invited_by_id=admin.id,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(invite)
    await db_session.commit()

    resp = await client.post("/auth/accept-invite", json={"token": "expired-token", "password": "S3cure-pass"})
    assert resp.status_code == 410


async def test_accept_unknown_invite(client):
    resp = await client.post("/auth/accept-invite", json={"token": "nope", "password": "S3cure-pass"})
    assert resp.status_code == 404


async def test_mfa_enrollment(client, make_user, db_session):
    user = await make_user("ENGINEER", mfa_enabled=False)
    headers = auth_headers(user)

    setup = await client.get("/auth/mfa/setup", headers=headers)
    assert setup.status_code == 200
    secret = setup.json()["secret"]

    bad = await client.post("/auth/mfa/verify", json={"code": "000000"}, headers=headers)
    assert bad.status_code == 400

    ok = await client.post("/auth/mfa/verify", json={"code": pyotp.TOTP(secret).now()}, headers=headers)
    assert ok.status_code == 200

    await db_session.refresh(user)
    assert (await db_session.get(User, user.id)).mfa_enabled is True


async def test_mfa_setup_forbidden_for_operator(client, make_user):
    user = await make_user("OPERATOR")
    resp = await client.get("/auth/mfa/setup", headers=auth_headers(user))
    assert resp.status_code == 403


async def test_update_language(client, make_user):
    user = await make_user()
    ok = await client.patch("/auth/me/language", json={"language": "fr"}, headers=auth_headers(user))
    assert ok.status_code == 200
    assert ok.json()["preferred_lang"] == "fr"

    bad = await client.patch("/auth/me/language", json={"language": "xx"}, headers=auth_headers(user))
    assert bad.status_code == 400
