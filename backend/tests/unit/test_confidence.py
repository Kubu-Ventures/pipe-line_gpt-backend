"""Unit tests for the citation-based confidence heuristic."""

from __future__ import annotations

import pytest

from app.services.llm import build_context_block, estimate_confidence


def _chunks(n: int, texts: dict[int, str] | None = None) -> list[dict]:
    chunks = [{"text_content": (texts or {}).get(i, f"chunk {i}")} for i in range(n)]
    build_context_block(chunks)  # assigns SRC-001..SRC-00n
    return chunks


def test_narrow_fully_cited_answer_is_confident():
    # Cites 2 of 6 retrieved sources; every figure is cited. The old ratio scored this 0.33.
    answer = (
        "## Incidents in 2024\n\n"
        "Based on the retrieved context, these incidents were reported:\n\n"
        "### Incident 1\n"
        "- **Date:** June 22, 2024 [SOURCE_ID=SRC-001]\n"
        "- **Cause:** Excavation damage [SOURCE_ID=SRC-001]\n\n"
        "### Incident 2\n"
        "- **Date:** July 5, 2024 [SOURCE_ID=SRC-005]\n"
        "- **Pressure:** 707 psig against a MAOP of 718 psig [SRC-005]\n\n"
        "> Note: this list reflects only the retrieved documents."
    )
    assert estimate_confidence(answer, _chunks(6)) == 1.0


def test_uncited_figures_lower_confidence():
    answer = "Pressure was 586 psig [SRC-001].\nMAOP was 594 psig.\nInstalled in 1930.\nIt failed in 2014."
    assert estimate_confidence(answer, _chunks(6)) == pytest.approx(0.25)


def test_no_citations_is_zero():
    assert estimate_confidence("The pipeline was installed in 1965.", _chunks(3)) == 0.0


def test_citation_to_unretrieved_source_is_penalized():
    answer = "Installed in 1965 [SRC-001].\nInstalled in 1989 [SRC-009]."
    # One of two claims backed, and one of two citations valid.
    assert estimate_confidence(answer, _chunks(3)) == pytest.approx(0.25)


def test_citation_lists_are_parsed():
    answer = "Both incidents occurred in 2014 [SRC-001, SRC-002]."
    assert estimate_confidence(answer, _chunks(2)) == 1.0


def test_cited_answer_without_figures_is_confident():
    assert estimate_confidence("The cause was excavation damage [SRC-002].", _chunks(4)) == 1.0


def test_no_chunks_keeps_neutral_score():
    assert estimate_confidence("Anything [SRC-001].", []) == 0.5


def test_citation_after_a_list_covers_the_list():
    # Claude often cites once below a list; every row used to count as unsupported (scored 0.0).
    answer = (
        "### 1. Merriam, Kansas\n"
        "- **Date:** August 12, 2014\n"
        "- **Pipe Installation Year:** 1989\n\n"
        "[SOURCE_ID=SRC-001]\n\n"
        "---\n\n"
        "| Parameter | Value |\n|---|---|\n| **Accident Pressure** | 458 psig |\n| **MAOP** | 690 psig |\n\n"
        "[SRC-002]"
    )
    assert estimate_confidence(answer, _chunks(6)) == 1.0


def test_cited_intro_covers_the_list_after_it():
    answer = (
        "The foreman took the following immediate actions [SRC-001]:\n\n"
        "1. Called local emergency response at 10:28 AM.\n"
        "2. Notified SSCGP."
    )
    assert estimate_confidence(answer, _chunks(6)) == 1.0


def test_cited_heading_covers_its_section():
    answer = "### Incident 1: Wakita, OK (2014) [SRC-001]\n\n| Pressure | 630 |\n| MAOP | 765 |\n\n### Other\n\nMAOP was 594 psig."
    assert estimate_confidence(answer, _chunks(6)) == pytest.approx(2 / 3)


def test_uncited_figures_found_in_sources_count_as_backed():
    sources = {0: "Report Number: 20140094 Local Datetime: 8/12/2014 Installed Year: 1989"}
    answer = "The leak was external corrosion [SRC-001].\n\n| 20140094 | Aug 12, 2014 | 1,989 |\n| 20150009 | Dec 24, 2014 | 1965 |"
    # Row 1's figures all appear in the sources; row 2's do not.
    assert estimate_confidence(answer, _chunks(6, sources)) == pytest.approx(0.5)


def test_list_numbering_is_not_a_figure():
    assert estimate_confidence("Cause: corrosion [SRC-001].\n\n1. Leak\n2. Rupture", _chunks(2)) == 1.0


def test_cited_quote_after_a_table_covers_it():
    answer = (
        "| **Accident Pressure** | 630 PSIG |\n| **Pressure vs. MAOP** | ~82% of MAOP |\n\n"
        '> *"Pressure did not exceed MAOP"* [SRC-001]'
    )
    assert estimate_confidence(answer, _chunks(3)) == 1.0


def test_cfr_references_are_not_figures():
    answer = "The MAOP basis is cited [SRC-001].\n\nMAOP set under 49 CFR §192.619(a)(2) and 49 CFR Part 192."
    assert estimate_confidence(answer, _chunks(3)) == 1.0
