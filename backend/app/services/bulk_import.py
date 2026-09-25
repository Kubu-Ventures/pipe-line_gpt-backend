"""Queue every ingestible file under a folder, for loading an operator's archive.

Files are streamed (hashed and copied in blocks), so their size is limited only by
max_bytes, not by memory. Each file is committed and queued before the next one is
read, and files already in the knowledge base are skipped by SHA-256, so an
interrupted import can simply be run again.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.file_types import has_valid_magic, source_type_for
from app.models.db import Document
from app.services import audit_log, upload_store

FILENAME_MAX = 512  # Document.filename column width

# Outcomes reported per file.
QUEUED = "queued"
WOULD_QUEUE = "would queue"
DUPLICATE = "duplicate"
SKIPPED = "skipped"


def _inspect(path: Path, max_bytes: int) -> tuple[str | None, str]:
    """Return (source_type, sha256), or (None, reason the file is skipped)."""
    size = path.stat().st_size
    if size == 0:
        return None, "empty file"
    if size > max_bytes:
        return None, f"larger than {max_bytes // 1_048_576} MB"
    source_type = source_type_for(path.name)
    if source_type is None:
        return None, "unsupported file type"

    digest = hashlib.sha256()
    with path.open("rb") as f:
        head = f.read(1024 * 1024)
        if not has_valid_magic(source_type, head):
            return None, f"not a valid {path.suffix.lstrip('.').upper()} file"
        while head:
            digest.update(head)
            head = f.read(1024 * 1024)
    return source_type, digest.hexdigest()


def _list_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
    )


def _display_name(path: Path, root: Path) -> str:
    # The path inside the archive tells apart the many "report.pdf" files of different years.
    name = path.relative_to(root).as_posix()
    return name if len(name) <= FILENAME_MAX else name[-FILENAME_MAX:]


async def import_folder(
    db: AsyncSession,
    root: Path,
    *,
    dispatch: Callable[..., object],
    operator_id: str,
    max_bytes: int,
    summarize: bool = True,
    dry_run: bool = False,
    report: Callable[[str, str, str], None] = lambda _name, _outcome, _detail: None,
) -> Counter:
    """Queue the files under root. dispatch(document_id, source_type, filename, summarize=...)
    must return an object with an .id (the Celery task). summarize=False skips the per-document
    Claude summary. Returns a count of each outcome."""
    counts: Counter = Counter()
    seen_in_run: dict[str, str] = {}  # sha256 -> name
    root = await asyncio.to_thread(root.resolve)

    for path in await asyncio.to_thread(_list_files, root):
        name = _display_name(path, root)
        try:
            source_type, detail = await asyncio.to_thread(_inspect, path, max_bytes)
        except OSError as exc:
            source_type, detail = None, f"unreadable ({type(exc).__name__})"
        if source_type is None:
            counts[SKIPPED] += 1
            report(name, SKIPPED, detail)
            continue

        sha256 = detail
        # seen_in_run catches copies within the folder even in a dry run, which writes nothing.
        existing = (
            seen_in_run.get(sha256)
            or (await db.execute(select(Document.filename).where(Document.sha256_hash == sha256))).scalar()
        )
        if existing is not None:
            counts[DUPLICATE] += 1
            report(name, DUPLICATE, f"same content as {existing}")
            continue
        seen_in_run[sha256] = name
        if dry_run:
            counts[WOULD_QUEUE] += 1
            report(name, WOULD_QUEUE, source_type)
            continue

        doc_id = str(uuid.uuid4())
        await asyncio.to_thread(upload_store.save_from, doc_id, path)
        db.add(
            Document(
                id=uuid.UUID(doc_id),
                filename=name,
                source_type=source_type,
                sha256_hash=sha256,
                status="PENDING",
                operator_id=operator_id,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # Same content committed meanwhile, e.g. by an upload through the UI.
            await db.rollback()
            upload_store.remove(doc_id)
            counts[DUPLICATE] += 1
            report(name, DUPLICATE, "same content was just ingested")
            continue

        task = dispatch(doc_id, source_type, name, summarize=summarize)
        await audit_log.log_event(
            db,
            event_type="INGEST_SUBMITTED",
            target_id=doc_id,
            target_type="document",
            payload={
                "filename": name,
                "source_type": source_type,
                "task_id": task.id,
                "via": "bulk_import",
                "summarize": summarize,
            },
        )
        counts[QUEUED] += 1
        report(name, QUEUED, source_type)

    return counts
