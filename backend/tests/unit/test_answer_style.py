"""Unit tests for answer clean-up applied before an answer is classified and shown."""

from __future__ import annotations

import pytest

from app.services.llm import remove_em_dashes


@pytest.mark.parametrize(
    "text,expected",
    [
        ("called at 10:28 AM — within one minute [SRC-001]", "called at 10:28 AM, within one minute [SRC-001]"),
        ("excavation damage—second party", "excavation damage, second party"),
        ("## Line XC Rupture – June 2024", "## Line XC Rupture, June 2024"),
        ("| Exceeded MAOP? | No – 458 psig |", "| Exceeded MAOP? | No, 458 psig |"),
        ("— Shutdown at 12:47 PM", "Shutdown at 12:47 PM"),
        ("> — Note", "> Note"),
        # En dashes in ranges are kept
        ("installed 1930–1989, pages 10–12", "installed 1930–1989, pages 10–12"),
        ("No dashes here.", "No dashes here."),
    ],
)
def test_remove_em_dashes(text, expected):
    assert remove_em_dashes(text) == expected
