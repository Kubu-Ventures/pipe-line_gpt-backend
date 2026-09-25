"""Staging directory for uploaded files waiting to be ingested.

Files reach the Celery worker through this directory rather than inside the Redis
message, so a backlog of thousands of documents costs disk space instead of broker
memory. The API, the bulk importer and the worker must all see the same directory
(a shared volume in Docker). The worker removes a file once its document has been
ingested or has failed for good.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from app.config import settings


def path_for(document_id: str) -> Path:
    # Parsing as a UUID also rules out path traversal.
    return Path(settings.upload_dir) / str(uuid.UUID(document_id))


def _write_atomically(target: Path, write) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    try:
        write(partial)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def save(document_id: str, content: bytes) -> None:
    _write_atomically(path_for(document_id), lambda p: p.write_bytes(content))


def save_from(document_id: str, source: Path) -> None:
    """Copy a file into staging without reading it all into memory."""
    _write_atomically(path_for(document_id), lambda p: shutil.copyfile(source, p))


def read(document_id: str) -> bytes:
    return path_for(document_id).read_bytes()


def remove(document_id: str) -> None:
    path_for(document_id).unlink(missing_ok=True)
