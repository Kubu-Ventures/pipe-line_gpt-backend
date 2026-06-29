from __future__ import annotations

import asyncio
import uuid

from celery import Celery
from app.config import settings

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
)



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
    import base64

    content = base64.b64decode(content_b64)
    try:
        return asyncio.run(_ingest_async(document_id, source_type, filename, content))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30)


_engine = None
_AsyncSessionLocal = None


def _get_session_factory():
    global _engine, _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker
        _engine = create_async_engine(settings.database_url, echo=False, pool_size=2, max_overflow=2)
        _AsyncSessionLocal = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _AsyncSessionLocal


async def _ingest_async(
    document_id: str,
    source_type: str,
    filename: str,
    content: bytes,
) -> dict:
    from app.ingest.csv_loader import load_csv
    from app.ingest.pdf_loader import load_pdf
    from app.ingest.phmsa_loader import load_phmsa_tsv, load_phmsa_zip
    from app.models.db import Chunk, Document
    from app.services.embedder import embed_texts

    AsyncSessionLocal = _get_session_factory()

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        result = await db.execute(select(Document).where(Document.id == uuid.UUID(document_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            return {"error": "Document not found"}

        doc.status = "PROCESSING"
        await db.commit()

        try:
            if source_type == "pdf":
                raw_chunks, meta = load_pdf(content, filename)
            elif source_type == "csv":
                raw_chunks, meta = load_csv(content, filename)
            elif source_type == "phmsa_zip":
                all_results = load_phmsa_zip(content)
                raw_chunks = []
                for chunks, meta, _ in all_results:
                    raw_chunks.extend(chunks)
                meta = {"source_type": "phmsa"}
            else:
                raw_chunks, meta = load_phmsa_tsv(content, filename)

            if not raw_chunks:
                doc.status = "FAILED"
                await db.commit()
                return {"error": "No chunks extracted"}

            # Update document metadata from loader
            if "year_from" in meta:
                doc.year_from = meta["year_from"]
            if "year_to" in meta:
                doc.year_to = meta["year_to"]
            if "commodity" in meta:
                doc.commodity = meta["commodity"]

            # Embed in batches of 100
            texts = [c["text_content"] for c in raw_chunks]
            embeddings: list[list[float]] = []
            batch_size = 100
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                batch_embeddings = await embed_texts(batch)
                embeddings.extend(batch_embeddings)

            # Persist chunks
            for raw_chunk, embedding in zip(raw_chunks, embeddings):
                chunk = Chunk(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    chunk_index=raw_chunk["chunk_index"],
                    text_content=raw_chunk["text_content"],
                    token_count=raw_chunk["token_count"],
                    page_ref=raw_chunk.get("page_ref"),
                    section_label=raw_chunk.get("section_label"),
                    embedding=embedding,
                )
                db.add(chunk)

            doc.chunk_count = len(raw_chunks)
            doc.status = "COMPLETED"
            await db.commit()

            return {"document_id": document_id, "chunk_count": len(raw_chunks), "status": "COMPLETED"}

        except Exception as exc:
            doc.status = "FAILED"
            await db.commit()
            raise exc
