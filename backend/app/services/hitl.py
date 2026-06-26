from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db import HITLReview, Query, Response


# Keywords that trigger HIGH risk routing
HIGH_RISK_VERBS = re.compile(
    r"\b(repair|shut[\s-]?in|shut[\s-]?down|reduce\s+pressure|pressure\s+reduction|"
    r"evacuate|evacuation|emergency\s+shutdown|isolate|bypass|depressuri[sz]e)\b",
    re.IGNORECASE,
)

FATALITY_PATTERN = re.compile(
    r"\b(fatal|fatality|fatalities|death|deaths|died|killed|injur|casualt)\b",
    re.IGNORECASE,
)

MEDIUM_RISK_KEYWORDS = re.compile(
    r"\b(maintenance|schedule\s+inspection|hca|high\s+consequence\s+area|"
    r"recommend\s+inspection|filing|compliance\s+deadline)\b",
    re.IGNORECASE,
)


def classify_risk(answer: str, confidence: float) -> tuple[str, bool]:
    """
    Returns (risk_level, hitl_required).
    risk_level: HIGH | MEDIUM | LOW
    """
    if HIGH_RISK_VERBS.search(answer) or FATALITY_PATTERN.search(answer):
        return "HIGH", True

    if MEDIUM_RISK_KEYWORDS.search(answer) or confidence < settings.hitl_confidence_threshold:
        return "MEDIUM", True

    return "LOW", False


async def queue_for_review(
    db: AsyncSession,
    response_obj: Response,
    query_obj: Query,
) -> HITLReview:
    """Create a pending HITLReview record for the given response."""
    review = HITLReview(
        id=uuid.uuid4(),
        response_id=response_obj.id,
        reviewer_id=None,
        decision=None,
        original_text=response_obj.answer_text,
        final_text=None,
        reason=None,
        reviewed_at=None,
    )
    db.add(review)

    query_obj.hitl_required = True
    query_obj.status = "UNDER_REVIEW"

    await db.commit()
    await db.refresh(review)
    return review


async def submit_review_decision(
    db: AsyncSession,
    review: HITLReview,
    reviewer_id: uuid.UUID,
    decision: str,
    final_text: str | None,
    reason: str | None,
) -> HITLReview:
    """Record the engineer's APPROVE / EDIT / REJECT decision."""
    if decision not in ("APPROVE", "EDIT", "REJECT"):
        raise ValueError(f"Invalid decision: {decision}")

    review.reviewer_id = reviewer_id
    review.decision = decision
    review.reason = reason
    review.reviewed_at = datetime.now(timezone.utc)

    if decision == "APPROVE":
        review.final_text = review.original_text
        review.response.query.status = "DELIVERED"
    elif decision == "EDIT":
        if not final_text:
            raise ValueError("final_text is required for EDIT decision")
        review.final_text = final_text
        review.response.query.status = "DELIVERED"
    else:  # REJECT
        review.final_text = None
        review.response.query.status = "REJECTED"

    await db.commit()
    await db.refresh(review)
    return review
