from __future__ import annotations

import csv
import io

from app.ingest.chunker import chunk_text


def load_csv(content: bytes, filename: str) -> tuple[list[dict], dict]:
    """
    Parse SCADA or annual report CSV into text chunks.
    Each row is serialised to a key:value line block, then chunked.
    """
    text_io = io.StringIO(content.decode("utf-8", errors="replace"))
    reader = csv.DictReader(text_io)
    rows = list(reader)

    metadata: dict = {
        "filename": filename,
        "source_type": "csv",
        "row_count": len(rows),
    }

    # Serialise rows into blocks of ~20 rows each before chunking
    block_size = 20
    all_chunks: list[dict] = []
    global_index = 0

    for block_start in range(0, len(rows), block_size):
        block = rows[block_start : block_start + block_size]
        lines = []
        for row_num, row in enumerate(block, start=block_start + 1):
            parts = [f"{k}: {v}" for k, v in row.items() if v]
            lines.append(f"[ROW {row_num}] " + " | ".join(parts))
        block_text = "\n".join(lines)
        block_label = f"Rows {block_start + 1}-{block_start + len(block)}"

        chunks = chunk_text(block_text, section_label=block_label)
        for chunk in chunks:
            chunk["chunk_index"] = global_index
            global_index += 1
        all_chunks.extend(chunks)

    return all_chunks, metadata
