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
    content_b64: str | None = None,
) -> dict:
    """
    Process and embed a document asynchronously.
    The file is read from upload_store. content_b64 carries it inline only in messages
    queued by versions before 0.3, which may still be waiting in Redis after an upgrade.
    """
    from app.services import upload_store

    content = base64.b64decode(content_b64) if content_b64 is not None else None
    try:
        result = asyncio.run(_ingest_async(document_id, source_type, filename, content))
    except DocumentParseError as exc:
        upload_store.remove(document_id)
        return {"document_id": document_id, "status": "FAILED", "error": str(exc)}
    except Exception as exc:
        # Keep the staged file for the retry; drop it once retries are exhausted.
        if self.request.retries >= self.max_retries:
            upload_store.remove(document_id)
            raise
        raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc
    upload_store.remove(document_id)
    return result


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


def _read_staged(document_id: str, filename: str) -> bytes:
    from app.services import upload_store

    try:
        return upload_store.read(document_id)
    except FileNotFoundError as exc:
        # Usually UPLOAD_DIR is not shared between the API and the worker.
        logger.error("Staged file for %s not found at %s", document_id, upload_store.path_for(document_id))
        raise DocumentParseError(f"The uploaded file for {filename} is missing from the staging directory") from exc


async def _ingest_async(
    document_id: str,
    source_type: str,
    filename: str,
    content: bytes | None = None,
) -> dict:
    """Ingest one document. With content=None the file is read from upload_store."""
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
                if content is None:
                    content = _read_staged(document_id, filename)
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
