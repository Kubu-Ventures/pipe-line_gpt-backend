"""
Queue a whole folder of documents for ingestion (e.g. an operator's archive).

Usage:
    python bulk_import.py <folder> [--operator EMAIL] [--max-mb N] [--skip-summaries] [--dry-run]

Walks the folder recursively, skips hidden files, unsupported types and files already
in the knowledge base (same SHA-256), and queues the rest for the Celery workers. It
only queues: follow progress in the Documents page or Flower. Safe to re-run after an
interruption. In Docker, see deploy/README.md ("Importing an archive").
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


async def run(folder: Path, operator: str, max_mb: int, summarize: bool, dry_run: bool) -> int:
    from app.services.bulk_import import QUEUED, WOULD_QUEUE, import_folder  # noqa: PLC0415
    from app.tasks.celery_app import ingest_document_task  # noqa: PLC0415

    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    def report(name: str, outcome: str, detail: str) -> None:
        print(f"{outcome:<12} {name}  ({detail})", flush=True)

    try:
        async with factory() as session:
            counts = await import_folder(
                session,
                folder,
                dispatch=ingest_document_task.delay,
                operator_id=operator,
                max_bytes=max_mb * 1_048_576,
                summarize=summarize,
                dry_run=dry_run,
                report=report,
            )
    finally:
        await engine.dispose()

    if not counts:
        print("No files found.")
        return 0
    print("\nSummary: " + ", ".join(f"{n} {outcome}" for outcome, n in sorted(counts.items())))
    if counts[QUEUED]:
        print("The workers process queued files in the background.")
        if not summarize:
            print("Summaries skipped: these documents are searchable in chat but won't feed the dashboard's")
            print("attention items or document summaries.")
    elif counts[WOULD_QUEUE]:
        print("Dry run: nothing was queued.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue a folder of documents for ingestion.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--operator", default="bulk-import", help="recorded as the uploader (default: bulk-import)")
    parser.add_argument("--max-mb", type=int, default=200, help="skip files larger than this (default: 200)")
    parser.add_argument(
        "--skip-summaries",
        action="store_true",
        help="don't ask Claude to summarise each document for the dashboard (saves one AI call per document; "
        "the documents are still fully searchable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="list what would be queued, change nothing")
    args = parser.parse_args()

    if not args.folder.is_dir():
        print(f"Error: {args.folder} is not a folder.")
        return 1
    return asyncio.run(run(args.folder, args.operator, args.max_mb, not args.skip_summaries, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
