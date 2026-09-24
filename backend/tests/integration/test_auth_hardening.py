"""MFA enforcement, token scopes, suspension, and brute-force lockout."""

from __future__ import annotations

import time

import pyotp

from tests.integration.helpers import DEFAULT_PASSWORD, auth_headers


async def _login(client, email, password=DEFAULT_PASSWORD, totp_code=None):
    body = {"email": email, "password": password}
    if totp_code:
        body["totp_code"] = totp_code
    return await client.post("/auth/login", json=body)


async def _enrolled_engineer(make_user, db_session):
    user = await make_user("ENGINEER", mfa_enabled=True)
    user.mfa_secret = pyotp.random_base32()
    await db_session.commit()
    return user


async def test_enrolled_user_needs_totp(client, make_user, db_session):
    user = await _enrolled_engineer(make_user, db_session)

    missing = await _login(client, user.email)
    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "mfa_required"

    wrong = await _login(client, user.email, totp_code="000000")
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["code"] == "mfa_invalid"

    code = pyotp.TOTP(user.mfa_secret).now()
    ok = await _login(client, user.email, totp_code=code)
    assert ok.status_code == 200
    assert ok.json()["mfa_setup_required"] is False

    replay = await _login(client, user.email, totp_code=code)
    assert replay.status_code == 401


async def test_unenrolled_engineer_gets_setup_only_token(client, make_user):
    user = await make_user("ENGINEER", mfa_enabled=False)
    resp = await _login(client, user.email)
    assert resp.json()["mfa_setup_required"] is True
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    assert (await client.get("/auth/me", headers=headers)).status_code == 200
    assert (await client.get("/auth/mfa/setup", headers=headers)).status_code == 200
    blocked = await client.get("/review", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "MFA enrollment required"


async def test_full_enrollment_then_login(client, make_user):
    user = await make_user("ADMIN", mfa_enabled=False)
    setup_token = (await _login(client, user.email)).json()["access_token"]
    headers = {"Authorization": f"Bearer {setup_token}"}

    secret = (await client.get("/auth/mfa/setup", headers=headers)).json()["secret"]
    verify = await client.post("/auth/mfa/verify", json={"code": pyotp.TOTP(secret).now()}, headers=headers)
    assert verify.status_code == 200

    # The enrollment code is single-use; the next 30s step is also accepted (valid_window=1).
    ok = await _login(client, user.email, totp_code=pyotp.TOTP(secret).at(time.time() + 30))
    assert ok.status_code == 200
    full = {"Authorization": f"Bearer {ok.json()['access_token']}"}
    assert (await client.get("/admin/users", headers=full)).status_code == 200


async def test_operator_never_needs_mfa(client, make_user):
    user = await make_user("OPERATOR")
    resp = await _login(client, user.email)
    assert resp.json()["mfa_setup_required"] is False
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert (await client.get("/query/history", headers=headers)).status_code == 200


async def test_legacy_preflagged_mfa_only_bypasses_in_demo_mode(client, make_user, monkeypatch):
    # Demo/legacy rows: mfa_enabled=True but no secret.
    user = await make_user("ENGINEER", mfa_enabled=True)

    assert (await _login(client, user.email)).json()["mfa_setup_required"] is True

    monkeypatch.setattr("app.config.settings.demo_mode", True)
    assert (await _login(client, user.email)).json()["mfa_setup_required"] is False


async def test_suspended_user_token_stops_working(client, make_user, db_session):
    user = await make_user("OPERATOR")
    headers = auth_headers(user)
    assert (await client.get("/auth/me", headers=headers)).status_code == 200

    user.status = "SUSPENDED"
    await db_session.commit()
    assert (await client.get("/auth/me", headers=headers)).status_code == 403


async def test_lockout_after_repeated_failures(client, make_user, monkeypatch):
    monkeypatch.setattr("app.config.settings.login_max_failures", 3)
    user = await make_user("OPERATOR")
    for _ in range(3):
        assert (await _login(client, user.email, password="wrong-password")).status_code == 401

    locked = await _login(client, user.email)  # correct password, still locked
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0


async def test_success_clears_failure_counter(client, make_user, monkeypatch):
    monkeypatch.setattr("app.config.settings.login_max_failures", 3)
    user = await make_user("OPERATOR")
    for _ in range(2):
        await _login(client, user.email, password="wrong-password")
    assert (await _login(client, user.email)).status_code == 200
    for _ in range(2):
        await _login(client, user.email, password="wrong-password")
    assert (await _login(client, user.email)).status_code == 200


async def test_login_email_is_case_insensitive(client, make_user):
    user = await make_user("OPERATOR", email="mixed@example.com")
    assert (await _login(client, "MiXeD@Example.com")).status_code == 200
    assert user.email == "mixed@example.com"


async def test_failed_login_is_audited(client, make_user, db_session):
    from sqlalchemy import select

    from app.models.db import AuditEvent

    user = await make_user("OPERATOR")
    await _login(client, user.email, password="wrong-password")
    events = (
        await db_session.execute(select(AuditEvent).where(AuditEvent.event_type == "USER_LOGIN_FAILED"))
    ).scalars()
    assert [e.payload_json["reason"] for e in events] == ["bad_credentials"]


async def test_admin_resets_mfa(client, make_user, db_session):
    admin = await make_user("ADMIN", mfa_enabled=True)
    engineer = await _enrolled_engineer(make_user, db_session)

    resp = await client.post(f"/admin/users/{engineer.id}/reset-mfa", headers=auth_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["mfa_enabled"] is False
    assert (await _login(client, engineer.email)).json()["mfa_setup_required"] is True

    self_reset = await client.post(f"/admin/users/{admin.id}/reset-mfa", headers=auth_headers(admin))
    assert self_reset.status_code == 400


async def test_short_invite_password_rejected(client, make_user):
    admin = await make_user("ADMIN", mfa_enabled=True)
    token = (
        await client.post(
            "/admin/invite", json={"email": "n@example.com", "role": "OPERATOR"}, headers=auth_headers(admin)
        )
    ).json()["token"]
    resp = await client.post("/auth/accept-invite", json={"token": token, "password": "short-pass"})
    assert resp.status_code == 422
