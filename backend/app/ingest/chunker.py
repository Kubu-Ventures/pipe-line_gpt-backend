from __future__ import annotations

CHUNK_SIZE = 400  # target tokens per chunk
CHUNK_OVERLAP = 50  # overlap tokens between adjacent chunks


def _approx_tokens(text: str) -> int:
    """Approximate token count (1 token ≈ 4 chars for English)."""
    return max(1, len(text) // 4)


def chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    page_ref: str | None = None,
    section_label: str | None = None,
) -> list[dict]:
    """
    Split text into overlapping chunks.
    Returns list of dicts with text_content, token_count, page_ref, section_label.
    """
    words = text.split()
    if not words:
        return []

    # Estimate words per chunk based on token approximation
    words_per_token = 0.75
    words_per_chunk = int(chunk_size * words_per_token)
    words_overlap = int(overlap * words_per_token)

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(words):
        end = min(start + words_per_chunk, len(words))
        chunk_words = words[start:end]
        chunk_text_content = " ".join(chunk_words)

        chunks.append(
            {
                "chunk_index": chunk_index,
                "text_content": chunk_text_content,
                "token_count": _approx_tokens(chunk_text_content),
                "page_ref": page_ref,
                "section_label": section_label,
            }
        )

        if end == len(words):
            break

        start = end - words_overlap
        chunk_index += 1

    return chunks
