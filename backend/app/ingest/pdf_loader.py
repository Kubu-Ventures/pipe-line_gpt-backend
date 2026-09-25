from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess

from app.config import settings
from app.ingest.chunker import chunk_text

logger = logging.getLogger(__name__)

# A page with less extractable text than this is treated as a scan and OCR'd.
OCR_MIN_CHARS = 25
OCR_DPI = 300
# Longest rendered side; large-format sheets (e.g. alignment sheets) are scaled down to it.
OCR_MAX_PIXELS = 6000
OCR_PAGE_TIMEOUT_S = 180


def _tesseract_path() -> str | None:
    return shutil.which("tesseract")


def tesseract_available() -> bool:
    return _tesseract_path() is not None


def _ocr_page(page) -> str:
    """Render one pdfplumber page and read it with the tesseract binary."""
    dpi = min(OCR_DPI, OCR_MAX_PIXELS * 72 / max(page.width, page.height))
    image = page.to_image(resolution=dpi).original.convert("L")
    png = io.BytesIO()
    image.save(png, format="PNG")
    # Fixed arguments; ocr_languages is validated against tesseract's code format in config.
    result = subprocess.run(  # noqa: S603
        [_tesseract_path() or "tesseract", "stdin", "stdout", "-l", settings.ocr_languages, "--dpi", str(int(dpi))],
        input=png.getvalue(),
        capture_output=True,
        timeout=OCR_PAGE_TIMEOUT_S,
        check=True,
        # One thread per page: several Celery workers already share the CPUs.
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
    )
    return result.stdout.decode("utf-8", errors="replace")


def load_pdf(content: bytes, filename: str) -> tuple[list[dict], dict]:
    """
    Extract text from ILI report PDF using pdfplumber. Pages without a text layer
    (scans) are OCR'd with tesseract when it is installed; their chunks are labelled
    "Page N (OCR)" so reviewers know the text may contain recognition errors.
    Returns (chunks, metadata).
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("pdfplumber not installed. Run: pip install pdfplumber") from exc

    all_chunks: list[dict] = []
    metadata: dict = {"filename": filename, "source_type": "pdf", "pages": 0, "ocr_pages": 0}
    ocr = settings.ocr_enabled and tesseract_available()
    pages_without_text = 0

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        metadata["pages"] = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            section_label = f"Page {page_num}"
            if len(page_text.strip()) < OCR_MIN_CHARS:
                ocr_text = ""
                if ocr:
                    try:
                        ocr_text = _ocr_page(page)
                    except (subprocess.SubprocessError, OSError) as exc:
                        stderr = getattr(exc, "stderr", None) or b""
                        logger.warning(
                            "OCR failed on page %d of %s: %s %s",
                            page_num,
                            filename,
                            type(exc).__name__,
                            stderr.decode(errors="replace")[-300:],
                        )
                if len(ocr_text.strip()) > len(page_text.strip()):
                    page_text = ocr_text
                    section_label += " (OCR)"
                    metadata["ocr_pages"] += 1
                elif not page_text.strip():
                    pages_without_text += 1
            if not page_text.strip():
                continue
            page_chunks = chunk_text(
                page_text,
                page_ref=str(page_num),
                section_label=section_label,
            )
            all_chunks.extend(page_chunks)

    if pages_without_text and not ocr:
        reason = "disabled (OCR_ENABLED=false)" if not settings.ocr_enabled else "unavailable (tesseract not installed)"
        logger.warning("%s: %d page(s) have no text layer and OCR is %s", filename, pages_without_text, reason)

    # Re-index across all pages
    for i, chunk in enumerate(all_chunks):
        chunk["chunk_index"] = i

    return all_chunks, metadata
