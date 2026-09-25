"""create_admin.py argument handling (the database write is covered by integration tests)."""

from __future__ import annotations

import io

import pytest

import create_admin


@pytest.fixture
def created(monkeypatch):
    calls = []

    async def fake_create_admin(email, password):
        calls.append((email, password))

    monkeypatch.setattr(create_admin, "create_admin", fake_create_admin)
    return calls


def test_password_stdin_reads_first_line(monkeypatch, created):
    monkeypatch.setattr("sys.stdin", io.StringIO("a-long-enough-password\nignored\n"))
    create_admin.main(["ops@example.com", "--password-stdin"])
    assert created == [("ops@example.com", "a-long-enough-password")]


def test_password_stdin_strips_crlf(monkeypatch, created):
    monkeypatch.setattr("sys.stdin", io.StringIO("a-long-enough-password\r\n"))
    create_admin.main(["ops@example.com", "--password-stdin"])
    assert created[0][1] == "a-long-enough-password"


@pytest.mark.parametrize("stdin", ["short\n", ""])
def test_password_stdin_rejects_short_or_missing(monkeypatch, created, stdin):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    with pytest.raises(SystemExit, match="at least 12 characters"):
        create_admin.main(["ops@example.com", "--password-stdin"])
    assert created == []


def test_interactive_prompt_requires_matching_confirmation(monkeypatch, created):
    answers = iter(["a-long-enough-password", "a-different-password"])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda _prompt: next(answers))
    with pytest.raises(SystemExit, match="do not match"):
        create_admin.main(["ops@example.com"])
    assert created == []


def test_interactive_prompt_creates_admin(monkeypatch, created):
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda _prompt: "a-long-enough-password")
    create_admin.main(["ops@example.com"])
    assert created == [("ops@example.com", "a-long-enough-password")]
