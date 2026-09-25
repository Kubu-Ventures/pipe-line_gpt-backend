"""Unit tests for data ingestion loaders."""

from __future__ import annotations

import pytest

from app.ingest.chunker import chunk_text
from app.ingest.csv_loader import load_csv
from app.ingest.phmsa_loader import load_phmsa_tsv


def test_chunker_basic():
    text = " ".join([f"word{i}" for i in range(1000)])
    chunks = chunk_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk["text_content"]
        assert chunk["token_count"] > 0
        assert "chunk_index" in chunk


def test_chunker_short_text():
    chunks = chunk_text("Short text.")
    assert len(chunks) == 1
    assert chunks[0]["chunk_index"] == 0


def test_chunker_empty():
    chunks = chunk_text("")
    assert chunks == []


def test_csv_loader():
    csv_content = b"IYEAR,OPERATOR_ID,CAUSE,FATAL,INJURE,COST_CURRENT,LOCATION_STATE\n"
    csv_content += b"2020,12345,CORROSION,0,0,50000,TX\n"
    csv_content += b"2021,12345,EXCAVATION DAMAGE,0,1,75000,TX\n"
    chunks, meta = load_csv(csv_content, "test.csv")
    assert len(chunks) >= 1
    assert meta["row_count"] == 2
    assert meta["source_type"] == "csv"


def test_phmsa_tsv_loader():
    tsv_content = (
        b"IYEAR\tOPERATOR_ID\tOPERATOR_NAME\tCAUSE\tSUBCAUSE\tFATAL\tINJURE\t"
        b"COST_CURRENT\tLOCATION_CITY\tLOCATION_STATE\tCOMMODITY\n"
    )
    tsv_content += b"2022\t98765\tAcme Pipeline\tCORROSION\tEXTERNAL\t0\t0\t100000\tHouston\tTX\tCRUDE OIL\n"
    tsv_content += b"2022\t98765\tAcme Pipeline\tEQUIPMENT FAILURE\tVALVE\t1\t2\t500000\tDallas\tTX\tCRUDE OIL\n"

    chunks, meta = load_phmsa_tsv(tsv_content, "test.tsv")
    assert len(chunks) >= 2
    assert meta["source_type"] == "phmsa"
    assert meta["year_from"] == 2022
    assert meta["year_to"] == 2022
    assert meta["commodity"] == "CRUDE OIL"
    # Fatality row should be labelled
    fatality_chunks = [c for c in chunks if "Fatality" in (c.get("section_label") or "")]
    assert len(fatality_chunks) >= 1


def test_phmsa_tsv_dedup_hash():
    """Same content should produce the same SHA-256."""
    from app.services.embedder import content_hash

    content = b"some pipeline data"
    assert content_hash(content) == content_hash(content)
    assert content_hash(b"different") != content_hash(content)


@pytest.mark.parametrize(
    ("filename", "relative_path", "expected"),
    [
        ("report.pdf", None, "report.pdf"),
        ("C:\\scans\\report.pdf", None, "report.pdf"),  # directories in the filename are dropped
        ("report.pdf", "records/2009/ILI/report.pdf", "records/2009/ILI/report.pdf"),
        ("report.pdf", "records\\2009\\report.pdf", "records/2009/report.pdf"),
        ("report.pdf", "/../../etc/./2009/report.pdf", "etc/2009/report.pdf"),  # no traversal, no root
        ("report.pdf", "records/2009/other.pdf", "report.pdf"),  # path for a different file: ignored
        ("report.pdf", "", "report.pdf"),
    ],
)
def test_display_name(filename, relative_path, expected):
    from app.ingest.file_types import display_name

    assert display_name(filename, relative_path) == expected


def test_display_name_keeps_the_end_of_a_very_long_path():
    from app.ingest.file_types import NAME_MAX, display_name

    name = display_name("report.pdf", "a/" * 400 + "report.pdf")
    assert len(name) == NAME_MAX
    assert name.endswith("/report.pdf")
