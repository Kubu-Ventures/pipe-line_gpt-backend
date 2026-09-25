"""OCR of scanned PDF pages. Tesseract itself is faked, except in the last test, which
runs only where the binary is installed (CI and the Docker image)."""

from __future__ import annotations

import io
import logging
import shutil
import subprocess

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.ingest import pdf_loader

SCANNED_TEXT = "EXCAVATION REQUIRED AT GIRTH WELD 2310"
OCR_OUTPUT = "Metal loss anomaly at girth weld 2310, depth 42% of wall thickness.\n"


def scanned_pdf(text: str = SCANNED_TEXT, size=(1700, 2200), resolution: float = 200.0) -> bytes:
    """A page that is only an image, like a scanner produces: no text layer."""
    image = Image.new("L", size, 255)
    ImageDraw.Draw(image).text((100, 200), text, fill=0, font=ImageFont.load_default(size=48))
    buf = io.BytesIO()
    image.save(buf, format="PDF", resolution=resolution)
    return buf.getvalue()


def text_pdf(text: str = "Inline inspection summary for segment SEG-TX-4B, run of March 2024.") -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
        b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        + f"5 0 obj<</Length {len(stream)}>>stream\n".encode()
        + stream
        + b"\nendstream endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )


def merge(*pdfs: bytes) -> bytes:
    out = pdfium.PdfDocument.new()
    for content in pdfs:
        out.import_pages(pdfium.PdfDocument(content))
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


@pytest.fixture
def fake_tesseract(monkeypatch):
    calls: list[dict] = []

    def run(args, **kwargs):
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, stdout=OCR_OUTPUT.encode(), stderr=b"")

    monkeypatch.setattr(pdf_loader, "tesseract_available", lambda: True)
    monkeypatch.setattr(pdf_loader.subprocess, "run", run)
    return calls


def test_scanned_page_is_ocrd_and_labelled(fake_tesseract):
    chunks, meta = pdf_loader.load_pdf(scanned_pdf(), "scan.pdf")

    assert meta["ocr_pages"] == 1
    assert [c["section_label"] for c in chunks] == ["Page 1 (OCR)"]
    assert "girth weld 2310" in chunks[0]["text_content"]
    call = fake_tesseract[0]
    assert call["input"].startswith(b"\x89PNG")
    assert call["args"][0].endswith("tesseract")
    assert call["args"][1:5] == ["stdin", "stdout", "-l", "eng"]
    assert call["env"]["OMP_THREAD_LIMIT"] == "1"


def test_only_pages_without_text_are_ocrd(fake_tesseract):
    chunks, meta = pdf_loader.load_pdf(merge(text_pdf(), scanned_pdf()), "mixed.pdf")

    assert len(fake_tesseract) == 1
    assert meta == {"filename": "mixed.pdf", "source_type": "pdf", "pages": 2, "ocr_pages": 1}
    assert [c["section_label"] for c in chunks] == ["Page 1", "Page 2 (OCR)"]
    assert [c["chunk_index"] for c in chunks] == [0, 1]


def test_huge_sheet_is_rendered_at_bounded_size(fake_tesseract):
    # 600 px at 7.2 dpi = a 6000 pt (83 inch) square sheet.
    pdf_loader.load_pdf(scanned_pdf(size=(600, 600), resolution=7.2), "alignment-sheet.pdf")

    args = fake_tesseract[0]["args"]
    dpi = int(args[args.index("--dpi") + 1])
    assert dpi == int(pdf_loader.OCR_MAX_PIXELS * 72 / 6000)
    width, height = Image.open(io.BytesIO(fake_tesseract[0]["input"])).size
    assert max(width, height) <= pdf_loader.OCR_MAX_PIXELS


def test_failed_page_is_skipped_not_fatal(monkeypatch, caplog):
    outputs = iter([subprocess.CalledProcessError(1, "tesseract", stderr=b"Error opening data file"), OCR_OUTPUT])

    def run(args, **kwargs):
        result = next(outputs)
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(args, 0, stdout=result.encode(), stderr=b"")

    monkeypatch.setattr(pdf_loader, "tesseract_available", lambda: True)
    monkeypatch.setattr(pdf_loader.subprocess, "run", run)
    with caplog.at_level(logging.WARNING):
        chunks, meta = pdf_loader.load_pdf(merge(scanned_pdf(), scanned_pdf("SECOND PAGE")), "scan.pdf")

    assert meta["ocr_pages"] == 1
    assert [c["page_ref"] for c in chunks] == ["2"]
    assert "Error opening data file" in caplog.text


def test_without_tesseract_scans_yield_nothing_and_warn(monkeypatch, caplog):
    monkeypatch.setattr(pdf_loader, "tesseract_available", lambda: False)
    with caplog.at_level(logging.WARNING):
        chunks, meta = pdf_loader.load_pdf(scanned_pdf(), "scan.pdf")

    assert chunks == []
    assert meta["ocr_pages"] == 0
    assert "tesseract not installed" in caplog.text


def test_ocr_can_be_disabled(fake_tesseract, monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    chunks, _ = pdf_loader.load_pdf(scanned_pdf(), "scan.pdf")

    assert chunks == []
    assert fake_tesseract == []


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_real_tesseract_reads_a_scan():
    chunks, meta = pdf_loader.load_pdf(scanned_pdf(), "scan.pdf")

    assert meta["ocr_pages"] == 1
    text = " ".join(c["text_content"] for c in chunks).upper()
    assert "GIRTH WELD" in text
    assert "2310" in text
