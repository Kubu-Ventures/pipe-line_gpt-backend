from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.middleware.auth import RequireEngineer, get_db, get_current_user
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


@router.get("", response_model=list[ReviewQueueItem])
async def get_review_queue(
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    user: Annotated[User, Depends(RequireEngineer)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> list[ReviewQueueItem]:
    """Return paginated list of AI responses, filtered by status (PENDING/APPROVED/REJECTED/all)."""
    offset = (page - 1) * page_size

    stmt = (
        select(HITLReview)
        .options(
            selectinload(HITLReview.response).selectinload(Response.query)
        )
        .order_by(HITLReview.id.asc())
        .offset(offset)
        .limit(page_size)
    )

    if status == "PENDING":
        stmt = stmt.where(HITLReview.decision.is_(None))
    elif status == "APPROVED":
        stmt = stmt.where(HITLReview.decision == "APPROVE")
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
                    pass

        conf = response.confidence_score if response else 0.0
        if conf >= 0.7:
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
    query_id: str,
    body: ReviewDecisionRequest,
    request: Request,
    user: Annotated[User, Depends(RequireEngineer)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
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
        .where(Query.id == uuid.UUID(query_id))
        .options(
            selectinload(HITLReview.response).selectinload(Response.query)
        )
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
        event_type=f"HITL_{body.decision}",
        actor_id=str(user.id),
        target_id=str(updated_review.id),
        target_type="hitl_review",
        payload={
            "query_id": query_id,
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
