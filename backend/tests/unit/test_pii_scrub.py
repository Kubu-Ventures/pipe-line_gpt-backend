from __future__ import annotations

import pytest

pytest.importorskip("presidio_analyzer")
spacy = pytest.importorskip("spacy")
if not spacy.util.is_package("en_core_web_sm"):
    pytest.skip("en_core_web_sm not installed", allow_module_level=True)

from app.routers.query import _scrub_pii  # noqa: E402


def test_personal_data_is_removed():
    out = _scrub_pii("John Smith (john@acme.com, 555-123-4567) asked about corrosion")
    assert "John Smith" not in out
    assert "john@acme.com" not in out
    assert "555-123-4567" not in out


def test_integrity_context_is_kept():
    question = "What is the wall loss on SEG-TX-4B near Houston since 2024-03-15?"
    out = _scrub_pii(question)
    for term in ("SEG-TX-4B", "Houston", "2024-03-15"):
        assert term in out
