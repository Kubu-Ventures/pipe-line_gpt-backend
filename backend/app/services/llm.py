from __future__ import annotations

import re
from typing import AsyncIterator

import anthropic

from app.config import settings
from app.models.schemas import Citation

SYSTEM_PROMPT = """You are PipelineGPT, an expert AI assistant specialising in pipeline integrity engineering.
You have deep knowledge of ILI (In-Line Inspection) techniques, SCADA systems, PHMSA regulations,
corrosion mechanisms, fracture mechanics, and pipeline risk assessment.

You answer questions strictly based on the retrieved documents provided in the context.
For every factual claim, you MUST cite the source using the format [SOURCE_ID].
If the context does not contain enough information to answer, say so clearly.
Do not speculate beyond the provided context.
When recommending any action (repair, pressure reduction, inspection, shutdown), explicitly
flag it as a recommendation requiring qualified engineer review."""

EXPANSION_PROMPT = """Generate 2-3 alternative phrasings of the following pipeline integrity query.
Return only the alternative questions, one per line, no numbering or bullets.
Original query: {question}"""


async def expand_query(question: str) -> list[str]:
    """Use Claude to generate alternative phrasings for improved retrieval recall."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.llm_model,
        max_tokens=256,
        messages=[{"role": "user", "content": EXPANSION_PROMPT.format(question=question)}],
    )
    variants = response.content[0].text.strip().split("\n")
    return [v.strip() for v in variants if v.strip()]


def build_context_block(chunks: list[dict]) -> str:
    """Assemble retrieved chunks into a numbered context block with metadata tags."""
    parts = []
    for i, chunk in enumerate(chunks, start=1):
        source_id = f"SRC-{i:03d}"
        chunk["_source_id"] = source_id
        segment = chunk.get("segment_id") or "N/A"
        date = chunk.get("ingest_date", "")[:10] if chunk.get("ingest_date") else "N/A"
        section = chunk.get("section_label") or ""
        header = f"[SOURCE_ID={source_id}] [DATE={date}] [SEGMENT={segment}]"
        if section:
            header += f" [SECTION={section}]"
        parts.append(f"{header}\n{chunk['text_content']}")
    return "\n\n---\n\n".join(parts)


def estimate_confidence(answer: str, chunks: list[dict]) -> float:
    """
    Heuristic confidence: ratio of [SRC-NNN] citations present in the answer
    against the number of chunks provided.
    """
    if not chunks:
        return 0.5
    cited = set(re.findall(r"\[SOURCE_ID=SRC-\d+\]|\[SRC-\d+\]", answer))
    return min(1.0, len(cited) / max(1, len(chunks)))


def extract_citations(answer: str, chunks: list[dict]) -> list[Citation]:
    """Map [SRC-NNN] markers in the answer text back to source chunk metadata."""
    citations = []
    seen: set[str] = set()
    for chunk in chunks:
        sid = chunk.get("_source_id", "")
        if not sid:
            continue
        if sid in seen:
            continue
        if sid in answer or sid.replace("SOURCE_ID=", "") in answer:
            seen.add(sid)
            citations.append(
                Citation(
                    source_id=sid,
                    document_id=chunk["document_id"],
                    filename=chunk["filename"],
                    chunk_index=chunk["chunk_index"],
                    page_ref=chunk.get("page_ref"),
                    section_label=chunk.get("section_label"),
                    excerpt=chunk["text_content"][:300],
                )
            )
    return citations


async def stream_answer(
    question: str,
    context_block: str,
    language: str = "en",
) -> AsyncIterator[str]:
    """Yield answer tokens from Claude with source-grounded streaming."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    lang_instruction = ""
    if language and language != "en":
        lang_instruction = f"\nPlease respond in {language} while keeping source citation tags in English."

    user_message = (
        f"RETRIEVED CONTEXT:\n{context_block}\n\n"
        f"USER QUESTION: {question}{lang_instruction}"
    )

    async with client.messages.stream(
        model=settings.llm_model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            yield text
