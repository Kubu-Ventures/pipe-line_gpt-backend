from __future__ import annotations

import asyncio
import base64
import logging
import uuid

from celery import Celery

from app.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "pipelinegpt",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    # Re-queue work from a worker that dies mid-task instead of losing it.
    task_reject_on_worker_lost=True,
    # Large PDFs embed on CPU; bound runaway tasks.
    task_soft_time_limit=1800,
    task_time_limit=1900,
    result_expires=86_400,
)


class DocumentParseError(Exception):
    """The file itself can't be processed; retrying won't help."""


@celery_app.task(bind=True, name="tasks.ingest_document", max_retries=3)
def ingest_document_task(
    self,
    document_id: str,
    source_type: str,
    filename: str,
    content_b64: str,
) -> dict:
    """
    Process and embed a document asynchronously.
    Accepts content as base64-encoded string to survive Celery JSON serialisation.
    """
    content = base64.b64decode(content_b64)
    try:
        return asyncio.run(_ingest_async(document_id, source_type, filename, content))
    except DocumentParseError as exc:
        return {"document_id": document_id, "status": "FAILED", "error": str(exc)}
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc


def _parse(source_type: str, filename: str, content: bytes) -> tuple[list[dict], dict]:
    from app.ingest.csv_loader import load_csv
    from app.ingest.pdf_loader import load_pdf
    from app.ingest.phmsa_loader import load_phmsa_tsv, load_phmsa_zip

    try:
        if source_type == "pdf":
            return load_pdf(content, filename)
        if source_type == "csv":
            return load_csv(content, filename)
        if source_type == "phmsa_zip":
            raw_chunks: list[dict] = []
            for chunks, _meta, _ in load_phmsa_zip(content):
                raw_chunks.extend(chunks)
            for idx, chunk in enumerate(raw_chunks):
                chunk["chunk_index"] = idx
            return raw_chunks, {"source_type": "phmsa"}
        return load_phmsa_tsv(content, filename)
    except Exception as exc:
        raise DocumentParseError(f"Could not parse {filename}: {type(exc).__name__}") from exc


async def _ingest_async(
    document_id: str,
    source_type: str,
    filename: str,
    content: bytes,
) -> dict:
    import redis.asyncio as aioredis
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.models.db import Chunk, Document
    from app.services import audit_log, semantic_cache
    from app.services.embedder import embed_texts

    # Each Celery task runs in a fresh event loop (asyncio.run), so connections must not
    # outlive it: a per-task engine without pooling, disposed at the end.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    doc_uuid = uuid.UUID(document_id)

    try:
        async with session_factory() as db:
            doc = (await db.execute(select(Document).where(Document.id == doc_uuid))).scalar_one_or_none()
            if not doc:
                return {"error": "Document not found"}

            doc.status = "PROCESSING"
            # A retry starts clean: drop chunks a previous attempt may have written.
            await db.execute(delete(Chunk).where(Chunk.document_id == doc_uuid))
            await db.commit()

            try:
                raw_chunks, meta = _parse(source_type, filename, content)
                if not raw_chunks:
                    raise DocumentParseError(f"No text could be extracted from {filename}")

                for field in ("year_from", "year_to", "commodity"):
                    if field in meta:
                        setattr(doc, field, meta[field])

                texts = [c["text_content"] for c in raw_chunks]
                embeddings: list[list[float]] = []
                for i in range(0, len(texts), 100):
                    embeddings.extend(await embed_texts(texts[i : i + 100]))

                for raw_chunk, embedding in zip(raw_chunks, embeddings, strict=True):
                    db.add(
                        Chunk(
                            id=uuid.uuid4(),
                            document_id=doc.id,
                            chunk_index=raw_chunk["chunk_index"],
                            text_content=raw_chunk["text_content"],
                            token_count=raw_chunk["token_count"],
                            page_ref=raw_chunk.get("page_ref"),
                            section_label=raw_chunk.get("section_label"),
                            embedding=embedding,
                        )
                    )

                doc.chunk_count = len(raw_chunks)
                doc.status = "COMPLETED"

                try:
                    from app.services.insights import extract_document_insights

                    doc.insights_json = await extract_document_insights(filename, raw_chunks) or {}
                except Exception:
                    logger.warning("Insight extraction failed for %s", document_id, exc_info=True)
                    doc.insights_json = {}

                await db.commit()
            except Exception as exc:
                # Discard partial chunks, then record the failure on a clean transaction.
                await db.rollback()
                doc = await db.get(Document, doc_uuid)
                if doc is not None:
                    doc.status = "FAILED"
                    await db.commit()
                await audit_log.log_event(
                    db,
                    event_type="INGEST_FAILED",
                    target_id=document_id,
                    target_type="document",
                    payload={"filename": filename, "error_type": type(exc).__name__},
                )
                raise

            await audit_log.log_event(
                db,
                event_type="INGEST_COMPLETED",
                target_id=document_id,
                target_type="document",
                payload={"filename": filename, "chunk_count": len(raw_chunks), "source_type": source_type},
            )

        redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            await semantic_cache.invalidate(redis)
        finally:
            await redis.aclose()

        return {"document_id": document_id, "chunk_count": len(raw_chunks), "status": "COMPLETED"}
    finally:
        await engine.dispose()
