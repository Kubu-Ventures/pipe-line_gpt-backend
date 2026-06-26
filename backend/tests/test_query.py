"""Integration-level tests for the query router using httpx TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data


def test_query_requires_auth(client):
    response = client.post(
        "/query",
        json={
            "question": "How many incidents in Texas?",
            "session_id": "test-session-1",
        },
    )
    assert response.status_code == 403


def test_register_and_login(client):
    # Register
    reg = client.post(
        "/auth/register",
        json={
            "email": "testuser@example.com",
            "password": "securepassword123",
            "role": "OPERATOR",
        },
    )
    # May conflict if DB already has user; accept 201 or 409
    assert reg.status_code in (201, 409)

    # Login
    login = client.post(
        "/auth/login",
        json={"email": "testuser@example.com", "password": "securepassword123"},
    )
    if reg.status_code == 201:
        assert login.status_code == 200
        assert "access_token" in login.json()


def test_ingest_requires_auth(client):
    response = client.post("/ingest", files={"file": ("test.csv", b"a,b\n1,2", "text/csv")})
    assert response.status_code == 403


def test_review_queue_requires_engineer(client):
    response = client.get("/review")
    assert response.status_code == 403


def test_audit_log_requires_engineer(client):
    response = client.get("/audit")
    assert response.status_code == 403
