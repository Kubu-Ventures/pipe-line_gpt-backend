from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import RequireAdmin, get_db
from app.models.db import Invitation, User
from app.models.schemas import (
    InviteOut,
    InviteRequest,
    UserListResponse,
    UserOut,
    UserStatusUpdate,
)
from app.services import audit_log

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=UserListResponse)
async def list_users(
    actor: Annotated[User, Depends(RequireAdmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserListResponse:
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return UserListResponse(total=len(users), users=[UserOut.model_validate(u) for u in users])


@router.get("/invitations", response_model=list[InviteOut])
async def list_invitations(
    actor: Annotated[User, Depends(RequireAdmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[InviteOut]:
    now = datetime.now(UTC)
    result = await db.execute(
        select(Invitation)
        .where(Invitation.accepted_at.is_(None))
        .where(Invitation.expires_at > now)
        .order_by(Invitation.created_at.desc())
    )
    invites = result.scalars().all()
    return [InviteOut.model_validate(i) for i in invites]


@router.post("/invite", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: InviteRequest,
    request: Request,
    actor: Annotated[User, Depends(RequireAdmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    # Check email not already registered
    existing_user = await db.execute(select(User).where(User.email == body.email.lower()))
    if existing_user.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with email {body.email} already exists.",
        )

    # Check no unexpired pending invitation
    now = datetime.now(UTC)
    existing_invite = await db.execute(
        select(Invitation)
        .where(Invitation.email == body.email.lower())
        .where(Invitation.accepted_at.is_(None))
        .where(Invitation.expires_at > now)
    )
    if existing_invite.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active invitation for {body.email} already exists.",
        )

    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(hours=48)

    invite = Invitation(
        id=uuid.uuid4(),
        email=body.email.lower(),
        role=body.role,
        token=token,
        invited_by_id=actor.id,
        expires_at=expires_at,
    )
    db.add(invite)
    await db.commit()

    await audit_log.log_event(
        db,
        event_type="USER_INVITED",
        actor_id=str(actor.id),
        target_id=body.email.lower(),
        target_type="invitation",
        payload={"email": body.email, "role": body.role},
        ip_address=request.client.host if request.client else None,
    )

    return {
        "message": f"Invitation created for {body.email}",
        "token": token,
        "expires_at": expires_at.isoformat(),
        "invite_url": f"/accept-invite/{token}",
    }


@router.patch("/users/{user_id}/status", response_model=UserOut)
async def update_user_status(
    user_id: uuid.UUID,
    body: UserStatusUpdate,
    request: Request,
    actor: Annotated[User, Depends(RequireAdmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if user.id == actor.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot change your own account status.")

    old_status = user.status
    user.status = body.status
    await db.commit()
    await db.refresh(user)

    await audit_log.log_event(
        db,
        event_type="USER_STATUS_CHANGED",
        actor_id=str(actor.id),
        target_id=str(user.id),
        target_type="user",
        payload={"email": user.email, "old_status": old_status, "new_status": body.status},
        ip_address=request.client.host if request.client else None,
    )

    return UserOut.model_validate(user)


@router.post("/users/{user_id}/reset-mfa", response_model=UserOut)
async def reset_user_mfa(
    user_id: uuid.UUID,
    request: Request,
    actor: Annotated[User, Depends(RequireAdmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    """Clear a user's TOTP enrollment (lost device). They must re-enroll at next sign-in."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if user.id == actor.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ask another admin to reset your MFA.")

    user.mfa_enabled = False
    user.mfa_secret = None
    await db.commit()
    await db.refresh(user)

    await audit_log.log_event(
        db,
        event_type="USER_MFA_RESET",
        actor_id=str(actor.id),
        target_id=str(user.id),
        target_type="user",
        payload={"email": user.email},
        ip_address=request.client.host if request.client else None,
    )
    return UserOut.model_validate(user)
