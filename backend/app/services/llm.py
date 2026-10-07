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


# A citation tag: [SRC-001], [SOURCE_ID=SRC-001], or a list such as [SRC-001, SRC-004].
_CITATION_TAG_RE = re.compile(r"\[[^\[\]]*\bSRC-\d+[^\[\]]*\]")
_SOURCE_ID_RE = re.compile(r"\bSRC-\d+")
_HEADING_RE = re.compile(r"^\s*#")
_TABLE_ROW_RE = re.compile(r"^\s*\|")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_THOUSANDS_SEP_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")
_NUMBER_RE = re.compile(r"\d+")
# A regulation reference such as "49 CFR §192.619(a)(2)" or "49 CFR Part 192" is not a claim.
_CFR_REF_RE = re.compile(r"\b\d+\s+CFR\b(?:\s+(?:Part\s+)?§*\s*[\d.]+(?:\([0-9a-zA-Z]+\))*)?", re.IGNORECASE)


def _cited_ids(text: str) -> list[str]:
    return [sid for tag in _CITATION_TAG_RE.findall(text) for sid in _SOURCE_ID_RE.findall(tag)]


def _figures(text: str) -> set[str]:
    """Digit runs in `text`, ignoring citation tags, a leading list marker, CFR references and thousands separators."""
    text = _CFR_REF_RE.sub("", _LIST_ITEM_RE.sub("", _CITATION_TAG_RE.sub("", text), count=1))
    return set(_NUMBER_RE.findall(_THOUSANDS_SEP_RE.sub("", text)))


def _blocks(answer: str) -> list[tuple[str, list[str]]]:
    """
    Split an answer into (kind, lines) blocks: "heading", "table" (consecutive rows),
    "list" (consecutive items with indented continuations), "cite" (a line holding only
    citation tags) or "prose" (one line). Blank lines separate blocks and are dropped.
    """
    blocks: list[tuple[str, list[str]]] = []
    for line in answer.splitlines():
        if not line.strip():
            blocks.append(("blank", []))
            continue
        if _HEADING_RE.match(line):
            kind = "heading"
        elif _TABLE_ROW_RE.match(line):
            kind = "table"
        elif _LIST_ITEM_RE.match(line) or (line[:1].isspace() and blocks and blocks[-1][0] == "list"):
            kind = "list"
        elif _cited_ids(line) and not _CITATION_TAG_RE.sub("", line).strip(" .,;:"):
            kind = "cite"
        else:
            kind = "prose"
        if kind in ("table", "list") and blocks and blocks[-1][0] == kind:
            blocks[-1][1].append(line)
        else:
            blocks.append((kind, [line]))
    return [block for block in blocks if block[0] != "blank"]


def estimate_confidence(answer: str, chunks: list[dict]) -> float:
    """
    Heuristic confidence: how much of the answer is backed by the retrieved sources.

    A "claim" is an answer line that states a figure (a digit outside citation tags and
    list numbering): dates, pressures, MAOPs, report numbers. Headings and lines without
    figures are ignored. A claim is backed when its block cites a retrieved source, where
    a citation covers the whole list or table it sits in, a citation on its own line
    (or any cited block right after a list or table) covers the block above it, a cited line ending in ":" covers the list or table after
    it, and a cited heading covers its section. A claim whose figures all appear in the
    retrieved text also counts as backed (e.g. an uncited summary table restating them).

    The score is the share of backed claims, scaled by the share of citations that point
    at a retrieved source. Citing only the relevant sources is not penalized.
    """
    if not chunks:
        return 0.5
    valid_ids = {chunk["_source_id"] for chunk in chunks if chunk.get("_source_id")}
    cited = _cited_ids(answer)
    if not cited:
        return 0.0
    valid_share = sum(sid in valid_ids for sid in cited) / len(cited)
    source_figures = set().union(*(_figures(chunk.get("text_content") or "") for chunk in chunks))

    def cites_valid(lines: list[str]) -> bool:
        return any(sid in valid_ids for line in lines for sid in _cited_ids(line))

    blocks = _blocks(answer)
    claims = backed = 0
    section_cited = False
    for i, (kind, lines) in enumerate(blocks):
        if kind == "heading":
            section_cited = cites_valid(lines)
            continue
        prev_kind, prev_lines = blocks[i - 1] if i > 0 else ("", [])
        next_kind, next_lines = blocks[i + 1] if i + 1 < len(blocks) else ("", [])
        supported = (
            section_cited
            or cites_valid(lines)
            or ((next_kind == "cite" or kind in ("list", "table")) and cites_valid(next_lines))
            or (
                kind in ("list", "table")
                and prev_kind == "prose"
                and cites_valid(prev_lines)
                and _CITATION_TAG_RE.sub("", prev_lines[-1]).rstrip().endswith(":")
            )
        )
        for line in lines:
            figures = _figures(line)
            if not figures:
                continue
            claims += 1
            backed += supported or figures <= source_figures
    claim_share = backed / claims if claims else 1.0
    return claim_share * valid_share


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
