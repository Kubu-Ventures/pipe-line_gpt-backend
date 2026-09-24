from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db import Chunk, Document
from app.models.schemas import QueryFilters


async def retrieve_chunks(
    db: AsyncSession,
    query_embedding: list[float],
    filters: QueryFilters | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """Retrieve top-k most similar chunks using pgvector cosine similarity."""
    k = top_k or settings.top_k_retrieval

    # Build the base query using pgvector operator <=> (cosine distance)
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.chunk_index,
            Chunk.text_content,
            Chunk.token_count,
            Chunk.page_ref,
            Chunk.section_label,
            Document.filename,
            Document.source_type,
            Document.segment_id,
            Document.ingest_date,
            Document.commodity,
            Document.year_from,
            Document.year_to,
            (1 - Chunk.embedding.cosine_distance(query_embedding)).label("similarity"),
        )
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.embedding.is_not(None))
    )

    if filters:
        if filters.pipeline_segment:
            stmt = stmt.where(Document.segment_id == filters.pipeline_segment)
        if filters.commodity:
            stmt = stmt.where(Document.commodity == filters.commodity)
        if filters.date_range:
            year_from, year_to = filters.date_range
            stmt = stmt.where(Document.year_from >= year_from, Document.year_to <= year_to)

    stmt = stmt.order_by(text("similarity DESC")).limit(k)

    result = await db.execute(stmt)
    rows = result.fetchall()

    chunks = []
    for row in rows:
        chunks.append(
            {
                "id": str(row.id),
                "document_id": str(row.document_id),
                "chunk_index": row.chunk_index,
                "text_content": row.text_content,
                "token_count": row.token_count,
                "page_ref": row.page_ref,
                "section_label": row.section_label,
                "filename": row.filename,
                "source_type": row.source_type,
                "segment_id": row.segment_id,
                "ingest_date": row.ingest_date.isoformat() if row.ingest_date else None,
                "commodity": row.commodity,
                "year_from": row.year_from,
                "year_to": row.year_to,
                "similarity": float(row.similarity),
            }
        )
    return chunks


def rerank_chunks(query: str, chunks: list[dict], top_k: int | None = None) -> list[dict]:
    """Rerank retrieved chunks using cross-encoder/ms-marco-MiniLM-L-6-v2."""
    k = top_k or settings.top_k_rerank
    if not chunks:
        return []

    try:
        from sentence_transformers import CrossEncoder

        model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        pairs = [(query, c["text_content"]) for c in chunks]
        scores = model.predict(pairs)
        ranked = sorted(zip(scores, chunks, strict=True), key=lambda x: x[0], reverse=True)
        return [c for _, c in ranked[:k]]
    except Exception:
        # Graceful fallback to similarity score ordering if cross-encoder unavailable
        return sorted(chunks, key=lambda c: c.get("similarity", 0), reverse=True)[:k]
