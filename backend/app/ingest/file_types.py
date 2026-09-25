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


NAME_MAX = 512  # Document.filename column width


def display_name(filename: str, relative_path: str | None = None) -> str:
    """The document's name: the file name, or its path inside an uploaded folder
    ("2009/ILI/report.pdf", as bulk import names files). Only ever a label; files are
    stored by document id. relative_path is ignored unless it ends with the file name."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1] or "upload"
    if relative_path:
        parts = [p for p in relative_path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        if parts and parts[-1] == base:
            name = "/".join(parts)
            return name if len(name) <= NAME_MAX else name[-NAME_MAX:]
    return base[:255]


def source_type_for(filename: str) -> str | None:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return EXTENSION_TO_SOURCE_TYPE.get(suffix)


def has_valid_magic(source_type: str, head: bytes) -> bool:
    magic = MAGIC_BYTES.get(source_type)
    return magic is None or head.startswith(magic)
