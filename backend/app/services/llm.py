from __future__ import annotations

import re
from collections.abc import AsyncIterator
from functools import lru_cache

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


LLMClient = anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrockMantle | anthropic.AsyncAnthropicVertex

_CLIENT_OPTIONS = {"timeout": 120.0, "max_retries": 2}


def make_client() -> LLMClient:
    """New client for the configured provider. All three expose the same messages API.

    Celery tasks each run in their own event loop, so they must call this (and close the
    client) rather than share the cached `get_client()` of the API process.
    """
    if settings.llm_provider == "bedrock":
        # Credentials: the standard AWS chain (instance/task role, AWS_PROFILE, AWS_ACCESS_KEY_ID).
        return anthropic.AsyncAnthropicBedrockMantle(aws_region=settings.aws_region, **_CLIENT_OPTIONS)
    if settings.llm_provider == "vertex":
        # Credentials: Google Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS).
        return anthropic.AsyncAnthropicVertex(
            project_id=settings.vertex_project_id, region=settings.vertex_region, **_CLIENT_OPTIONS
        )
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, **_CLIENT_OPTIONS)


@lru_cache(maxsize=1)
def get_client() -> LLMClient:
    """Shared client so HTTP connections are pooled across requests."""
    return make_client()


def _is_cloud_credential_error(exc: Exception) -> bool:
    """AWS/Google credential failures raise before any request is sent, outside the SDK's
    error hierarchy. Matched by module so neither library has to be importable."""
    # The SDK's own Bedrock auth raises a bare RuntimeError when the AWS chain finds nothing.
    if isinstance(exc, RuntimeError) and str(exc).startswith("Could not resolve AWS credentials"):
        return True
    for cls in type(exc).__mro__:
        module = cls.__module__
        if module.startswith("botocore.exceptions") and cls.__name__ in {
            "NoCredentialsError",
            "PartialCredentialsError",
            "NoRegionError",
            "TokenRetrievalError",
            "SSOTokenLoadError",
        }:
            return True
        if module.startswith("google.auth.exceptions"):
            return True
    return False


def user_facing_llm_error(exc: Exception) -> str:
    """Map SDK errors to a message safe to show operators (no raw upstream details)."""
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError) or _is_cloud_credential_error(
        exc
    ):
        return "The AI service is misconfigured (invalid API credentials). Contact your administrator."
    if isinstance(exc, anthropic.RateLimitError):
        return "The AI service is busy right now. Please try again in a minute."
    if isinstance(exc, anthropic.BadRequestError) and "credit" in str(exc).lower():
        return "The AI service account is out of credits. Contact your administrator."
    if isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError):
        return "Could not reach the AI service. Please try again."
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return "The AI service had a temporary error. Please try again."
    return "The answer could not be generated. Please try again."


def response_text(message: anthropic.types.Message) -> str:
    """The answer text of a response. Newer models (e.g. Sonnet 5, the Sonnet-class model on
    Bedrock) think by default, so content can start with a thinking block: never read content[0]."""
    return "".join(block.text for block in message.content if block.type == "text")


# Output caps leave room for models that think before answering; thinking counts
# toward max_tokens. They are ceilings, not costs: non-thinking models stop far earlier.
async def expand_query(question: str) -> list[str]:
    """Use Claude to generate alternative phrasings for improved retrieval recall."""
    response = await get_client().messages.create(
        model=settings.llm_model,
        max_tokens=4096,
        messages=[{"role": "user", "content": EXPANSION_PROMPT.format(question=question)}],
    )
    variants = response_text(response).strip().split("\n")
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


_LANG_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "ar": "Arabic",
    "zh": "Chinese (Simplified)",
    "ru": "Russian",
    "pt": "Portuguese",
    "de": "German",
    "ja": "Japanese",
    "hi": "Hindi",
}


async def stream_answer(
    question: str,
    context_block: str,
    language: str = "en",
    usage: dict[str, int] | None = None,
) -> AsyncIterator[str]:
    """Yield answer tokens from Claude. If `usage` is given, it receives input/output token counts."""

    lang_instruction = ""
    if language and language.lower() != "en":
        lang_name = _LANG_NAMES.get(language.lower(), language)
        lang_instruction = (
            f"\nIMPORTANT: You MUST respond entirely in {lang_name}. "
            f"All explanations, analysis, and recommendations must be written in {lang_name}. "
            "Only keep source citation tags (e.g. [SRC-001]) in their original format."
        )

    user_message = f"RETRIEVED CONTEXT:\n{context_block}\n\nUSER QUESTION: {question}{lang_instruction}"

    async with get_client().messages.stream(
        model=settings.llm_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            yield text
        if usage is not None:
            final = await stream.get_final_message()
            usage["input_tokens"] = final.usage.input_tokens
            usage["output_tokens"] = final.usage.output_tokens
