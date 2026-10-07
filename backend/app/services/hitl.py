from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db import HITLReview, Query, Response

# Operational actions that make a recommendation HIGH risk
HIGH_RISK_VERBS = re.compile(
    r"\b(repair|shut[\s-]?in|shut[\s-]?down|pressure\s+reduction|"
    r"(?:reduc|lower)(?:e|es|ing)?\s+(?:the\s+)?(?:operating\s+)?pressure|derat(?:e|ing)|"
    r"evacuate|evacuation|emergency\s+shutdown|isolate|bypass|depressuri[sz]e)\b",
    re.IGNORECASE,
)

# Harm to people: HIGH risk anywhere in the answer unless negated ("no fatalities", "Injuries: 0")
FATALITY_PATTERN = re.compile(
    r"\b(fatal|fatality|fatalities|death|deaths|died|killed|injur\w*|casualt\w*)\b",
    re.IGNORECASE,
)

# Keywords that make a recommendation MEDIUM risk
MEDIUM_RISK_KEYWORDS = re.compile(
    r"\b(maintenance|schedule\s+inspection|hca|high\s+consequence\s+area|"
    r"recommend\s+inspection|filing|compliance\s+deadline)\b",
    re.IGNORECASE,
)

# A clause that recommends something: a modal or advice verb, or an imperative opening ("Reduce ...")
RECOMMENDATION_CUES = re.compile(
    r"\b(should|must|ought\s+to|needs?\s+to|recommend\w*|advis\w*|suggest\w*|consider|considering|urge\w*|"
    r"it\s+is\s+(?:prudent|advisable|necessary|essential|critical))\b",
    re.IGNORECASE,
)
IMPERATIVE_START = re.compile(
    r"(?:repair|reduce|lower|shut|isolate|evacuate|depressuri[sz]e|bypass|derate|schedule|perform|conduct|"
    r"implement|initiate|replace|excavate|inspect|stop|halt|restrict|limit)\b"
    r"(?![\s*_]*:)(?!\s+(?:was|were|is|are|has|had|involved|included|of)\b)",
    re.IGNORECASE,
)
RECOMMENDATION_HEADING = re.compile(r"\b(recommend\w*|next\s+steps|action\s+(?:items|plan))\b", re.IGNORECASE)

# Deferral boilerplate, e.g. "Any decisions regarding repair scope should be reviewed by a qualified engineer"
DEFERRAL = re.compile(r"\bdecisions?\b.*\bshould\s+be\s+(?:reviewed|approved|made)\b.*\bengineer", re.IGNORECASE)

_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")
_LEADING_MARKUP_RE = re.compile(r"^[\s>*_+-]*(?:\d+[.)]\s*)?[\s*_]*")
_NEGATION_BEFORE_RE = re.compile(r"\b(?:no|zero|0|without|nor)\s+(?:[\w*-]+\s+){0,3}$", re.IGNORECASE)
_ZERO_COUNT_AFTER_RE = re.compile(r"^[\s*_]*[:=][\s*_]*(?:0|none|no)\b", re.IGNORECASE)


def _recommendation_clauses(answer: str) -> list[str]:
    """Clauses that recommend an action; factual description of past events is left out."""
    clauses = []
    in_recommendation_section = False
    for line in answer.splitlines():
        if line.lstrip().startswith("#"):
            in_recommendation_section = bool(RECOMMENDATION_HEADING.search(line))
            if in_recommendation_section:
                clauses.append(line)
            continue
        for clause in _CLAUSE_SPLIT_RE.split(line):
            if IMPERATIVE_START.match(_LEADING_MARKUP_RE.sub("", clause)):
                clauses.append(clause)
            elif not DEFERRAL.search(clause) and (in_recommendation_section or RECOMMENDATION_CUES.search(clause)):
                clauses.append(clause)
    return clauses


def _mentions_harm(answer: str) -> bool:
    for match in FATALITY_PATTERN.finditer(answer):
        before = answer[max(0, match.start() - 40) : match.start()]
        after = answer[match.end() : match.end() + 15]
        if not (_NEGATION_BEFORE_RE.search(before) or _ZERO_COUNT_AFTER_RE.match(after)):
            return True
    return False


def classify_risk(answer: str, confidence: float) -> tuple[str, bool]:
    """
    Returns (risk_level, hitl_required).
    risk_level: HIGH | MEDIUM | LOW

    Action keywords count only in clauses that recommend something, so an answer that
    describes a past repair or shutdown is not held for it. Any non-negated mention of
    deaths or injuries is HIGH regardless.
    """
    recommendations = _recommendation_clauses(answer)
    if any(HIGH_RISK_VERBS.search(c) for c in recommendations) or _mentions_harm(answer):
        return "HIGH", True

    if any(MEDIUM_RISK_KEYWORDS.search(c) for c in recommendations) or confidence < settings.hitl_confidence_threshold:
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
    review.reviewed_at = datetime.now(UTC)

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
