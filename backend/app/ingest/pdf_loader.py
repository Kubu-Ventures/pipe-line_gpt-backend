from __future__ import annotations

import io

from app.ingest.chunker import chunk_text


def load_pdf(content: bytes, filename: str) -> tuple[list[dict], dict]:
    """
    Extract text from ILI report PDF using pdfplumber.
    Returns (chunks, metadata).
    """
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber not installed. Run: pip install pdfplumber")

    all_chunks: list[dict] = []
    metadata: dict = {"filename": filename, "source_type": "pdf", "pages": 0}

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        metadata["pages"] = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            if not page_text.strip():
                continue
            page_chunks = chunk_text(
                page_text,
                page_ref=str(page_num),
                section_label=f"Page {page_num}",
            )
            all_chunks.extend(page_chunks)

    # Re-index across all pages
    for i, chunk in enumerate(all_chunks):
        chunk["chunk_index"] = i

    return all_chunks, metadata
