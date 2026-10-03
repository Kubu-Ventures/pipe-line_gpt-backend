"""Document tags: the pipeline segment and commodity that query filters match exactly."""

from __future__ import annotations

TAG_MAX = 100  # width of Document.segment_id and Document.commodity


def clean_tag(value: str | None) -> str | None:
    """Trim a tag someone typed and collapse inner whitespace; a blank tag means untagged."""
    if value is None:
        return None
    return " ".join(value.split()) or None
