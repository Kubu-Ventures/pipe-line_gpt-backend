from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import Query as QueryParam
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.middleware.auth import RequireEngineer, get_db
from app.models.db import HITLReview, Query, Response, User
from app.models.schemas import (
    Citation,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewQueueItem,
)
from app.services import audit_log
from app.services.hitl import submit_review_decision

router = APIRouter(prefix="/review", tags=["review"])
logger = logging.getLogger(__name__)

# Past-tense names are what the audit export and frontend expect.
HITL_EVENT_TYPES = {"APPROVE": "HITL_APPROVED", "EDIT": "HITL_EDITED", "REJECT": "HITL_REJECTED"}


@router.get("", response_model=list[ReviewQueueItem])
async def get_review_queue(
    user: Annotated[User, Depends(RequireEngineer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, QueryParam(ge=1)] = 1,
    page_size: Annotated[int, QueryParam(ge=1, le=100)] = 20,
    status: str | None = None,
) -> list[ReviewQueueItem]:
    """Paginated review items, oldest question first, filtered by PENDING/APPROVED/EDITED/REJECTED (or all)."""
    offset = (page - 1) * page_size

    stmt = (
        select(HITLReview)
        .join(Response, HITLReview.response_id == Response.id)
        .join(Query, Response.query_id == Query.id)
        .options(selectinload(HITLReview.response).selectinload(Response.query))
        .order_by(Query.query_ts.asc(), HITLReview.id.asc())
        .offset(offset)
        .limit(page_size)
    )

    if status == "PENDING":
        stmt = stmt.where(HITLReview.decision.is_(None))
    elif status == "APPROVED":
        stmt = stmt.where(HITLReview.decision == "APPROVE")
    elif status == "EDITED":
        stmt = stmt.where(HITLReview.decision == "EDIT")
    elif status == "REJECTED":
        stmt = stmt.where(HITLReview.decision == "REJECT")
    # else: no filter — return all
    result = await db.execute(stmt)
    reviews = result.scalars().all()

    items = []
    for review in reviews:
        response = review.response
        query = response.query if response else None
        citations = []
        if response and response.citations_json:
            for c in response.citations_json:
                try:
                    citations.append(Citation(**c))
                except Exception:
                    logger.warning("Skipping malformed citation on review %s", review.id, exc_info=True)

        conf = response.confidence_score if response else 0.0
        if response and response.risk_level:
            risk_level = response.risk_level
        elif conf >= 0.7:  # rows from before risk_level was stored
            risk_level = "LOW"
        elif conf >= 0.4:
            risk_level = "MEDIUM"
        else:
            risk_level = "HIGH"

        if review.decision is None:
            status = "PENDING"
        elif review.decision == "APPROVE":
            status = "APPROVED"
        elif review.decision == "REJECT":
            status = "REJECTED"
        else:
            status = review.decision

        items.append(
            ReviewQueueItem(
                id=review.id,
                response_id=review.response_id,
                query_id=query.id if query else uuid.uuid4(),
                question_raw=query.question_raw if query else "",
                answer_text=review.original_text or "",
                confidence_score=conf,
                citations_json=citations,
                risk_level=risk_level,
                status=status,
                decision=review.decision,
                created_at=review.reviewed_at or (query.query_ts if query else None),
            )
        )

    return items


@router.post("/{query_id}", response_model=ReviewDecisionResponse)
async def submit_decision(
    query_id: uuid.UUID,
    body: ReviewDecisionRequest,
    request: Request,
    user: Annotated[User, Depends(RequireEngineer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReviewDecisionResponse:
    """Submit APPROVE / EDIT / REJECT decision for a queued response."""
    if body.decision not in ("APPROVE", "EDIT", "REJECT"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="decision must be APPROVE, EDIT, or REJECT",
        )
    if body.decision == "EDIT" and not body.final_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="final_text is required for EDIT decision",
        )
    if body.decision == "REJECT" and not body.reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="reason is required for REJECT decision",
        )

    # Look up the review via query_id
    stmt = (
        select(HITLReview)
        .join(Response, HITLReview.response_id == Response.id)
        .join(Query, Response.query_id == Query.id)
        .where(Query.id == query_id)
        .options(selectinload(HITLReview.response).selectinload(Response.query))
        # Lock the row so two engineers deciding at once can't both succeed.
        .with_for_update(of=HITLReview)
    )
    result = await db.execute(stmt)
    review = result.scalar_one_or_none()

    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found for this query")

    if review.decision is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review already decided")

    updated_review = await submit_review_decision(
        db=db,
        review=review,
        reviewer_id=user.id,
        decision=body.decision,
        final_text=body.final_text,
        reason=body.reason,
    )

    await audit_log.log_event(
        db,
        event_type=HITL_EVENT_TYPES[body.decision],
        actor_id=str(user.id),
        target_id=str(updated_review.id),
        target_type="hitl_review",
        payload={
            "query_id": str(query_id),
            "decision": body.decision,
            "reason": body.reason,
            "has_edit": body.final_text is not None,
        },
        ip_address=request.client.host if request.client else None,
    )

    return ReviewDecisionResponse(
        review_id=updated_review.id,
        decision=body.decision,
        message=f"Response {body.decision.lower()}d successfully.",
    )
