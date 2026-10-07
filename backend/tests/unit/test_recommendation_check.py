"""The model's check for operational advice in an answer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import llm


def _fake_client(monkeypatch, reply=None, error=None):
    async def create(**_kwargs):
        if error:
            raise error
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=reply)],
            usage=SimpleNamespace(input_tokens=50, output_tokens=2),
        )

    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))


@pytest.mark.parametrize("reply,expected", [("NONE", False), ("none", False), ("ACTION", True), ("Unsure", True)])
async def test_reads_the_verdict_and_holds_when_unclear(monkeypatch, reply, expected):
    _fake_client(monkeypatch, reply=reply)
    usage = {"input_tokens": 100, "output_tokens": 20}
    assert await llm.check_recommends_action("q", "a", usage) is expected
    assert usage == {"input_tokens": 150, "output_tokens": 22}


async def test_holds_when_the_model_call_fails(monkeypatch):
    _fake_client(monkeypatch, error=RuntimeError("upstream down"))
    assert await llm.check_recommends_action("q", "a") is True
