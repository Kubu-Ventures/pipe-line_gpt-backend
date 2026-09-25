from __future__ import annotations

import json
import re
from datetime import date

from app.config import settings
from app.services.llm import make_client, response_text

_PROMPT = """You are a pipeline integrity analyst. Analyse the document chunks below and extract structured operational intelligence.

Return ONLY a valid JSON object — no markdown, no explanation, no code fences.

Schema:
{{
  "doc_type": "ILI_REPORT" | "SCADA" | "IMP_SCHEDULE" | "PHMSA" | "OTHER",
  "summary": "2-sentence plain-English summary of what this document contains",
  "deadlines": [
    {{
      "segment": "segment ID or location",
      "item": "what is due or overdue",
      "date": "YYYY-MM-DD or null",
      "status": "OVERDUE" | "DUE_SOON" | "UPCOMING",
      "days_until": <integer, negative means overdue>
    }}
  ],
  "anomalies": [
    {{
      "segment": "segment ID",
      "feature": "anomaly name and location",
      "severity": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW",
      "detail": "brief description including measurements where available",
      "action_required": true | false
    }}
  ],
  "alarms": [
    {{
      "tag": "alarm tag or name",
      "location": "location / segment",
      "date": "YYYY-MM-DD or null",
      "description": "what happened"
    }}
  ],
  "suggested_queries": [
    "<specific, actionable question 1 that references actual values, dates, or IDs from this document>",
    "<specific, actionable question 2>"
  ]
}}

Rules:
- suggested_queries MUST reference specific segment IDs, dates, measurements, or alarm tags found in the text.
- If a category has no items, return an empty array [].
- Dates in YYYY-MM-DD format. Today is {today}.
- Return ONLY the JSON object.

DOCUMENT CHUNKS:
{chunks}"""


# Stored as insights_json when a document was ingested without a summary (bulk-import
# --skip-summaries). Unlike NULL it is not filled in lazily by the dashboard, and the
# dashboard refresh leaves it alone, so skipping really saves the Claude calls.
SKIPPED: dict = {"skipped": True}


def is_skipped(insights: dict | None) -> bool:
    return bool(insights and insights.get("skipped"))


async def extract_document_insights(filename: str, chunks: list[dict]) -> dict:
    """
    Send the first 8 chunks to Claude and return structured intelligence as a dict.
    Returns {} on any failure so callers can treat it as optional enrichment.
    """
    sample = chunks[:8]
    chunk_text = "\n\n---\n\n".join(
        f"[Chunk {c.get('chunk_index', i)} | Section: {c.get('section_label') or 'N/A'}]\n{c['text_content']}"
        for i, c in enumerate(sample)
    )

    prompt = _PROMPT.format(chunks=chunk_text, today=date.today().isoformat())

    try:
        # Runs inside a Celery task's own event loop: use a fresh client, never the cached one.
        async with make_client() as client:
            response = await client.messages.create(
                model=settings.llm_model,
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
        raw = response_text(response).strip()
        # Strip markdown fences if the model added them despite instructions
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)
        return json.loads(raw)
    except Exception:
        return {}
