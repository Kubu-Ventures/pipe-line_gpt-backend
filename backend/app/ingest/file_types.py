"""Which files can be ingested, shared by the upload endpoint and the bulk importer."""

from __future__ import annotations

EXTENSION_TO_SOURCE_TYPE = {
    ".pdf": "pdf",
    ".csv": "csv",
    ".tsv": "phmsa",
    ".txt": "phmsa",
    ".zip": "phmsa_zip",
}

# Leading bytes a file of this source type must start with.
MAGIC_BYTES = {"pdf": b"%PDF-", "phmsa_zip": b"PK"}


def source_type_for(filename: str) -> str | None:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return EXTENSION_TO_SOURCE_TYPE.get(suffix)


def has_valid_magic(source_type: str, head: bytes) -> bool:
    magic = MAGIC_BYTES.get(source_type)
    return magic is None or head.startswith(magic)
