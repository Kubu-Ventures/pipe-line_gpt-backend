from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import (
    RequireEngineer,
    create_access_token,
    get_current_user,
    get_db,
    hash_password,
    verify_password,
)
from app.models.db import Invitation, User
from app.models.schemas import (
    AcceptInviteRequest,
    LoginRequest,
    MFASetupResponse,
    MFAVerifyRequest,
    TokenResponse,
    UserOut,
)
from app.services import audit_log

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if user.status == "SUSPENDED":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account suspended. Contact your administrator.",
        )

    user.last_login = datetime.now(timezone.utc)
    await db.commit()

    token = create_access_token(str(user.id), user.role)

    mfa_setup_required = user.role in ("ENGINEER", "ADMIN") and not user.mfa_enabled

    await audit_log.log_event(
        db,
        event_type="USER_LOGIN",
        actor_id=str(user.id),
        target_id=str(user.id),
        target_type="user",
        payload={"email": user.email, "mfa_setup_required": mfa_setup_required},
        ip_address=request.client.host if request.client else None,
    )

    return TokenResponse(access_token=token, mfa_setup_required=mfa_setup_required)


@router.get("/me", response_model=UserOut)
async def get_me(user: Annotated[User, Depends(get_current_user)]) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/accept-invite", status_code=status.HTTP_201_CREATED)
async def accept_invite(
    body: AcceptInviteRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    now = datetime.now(timezone.utc)

    result = await db.execute(select(Invitation).where(Invitation.token == body.token))
    invite = result.scalar_one_or_none()

    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found or already used.")

    if invite.accepted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This invitation has already been accepted.")

    expires = invite.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now > expires:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invitation has expired. Request a new one from your administrator.")

    if invite.email.lower() != body.email.lower():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email does not match invitation.")

    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.")

    user = User(
        id=uuid.uuid4(),
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        role=invite.role,
        status="ACTIVE",
        mfa_enabled=False,
    )
    db.add(user)

    invite.accepted_at = now
    await db.commit()
    await db.refresh(user)

    await audit_log.log_event(
        db,
        event_type="USER_INVITED_ACCEPTED",
        actor_id=str(user.id),
        target_id=str(user.id),
        target_type="user",
        payload={"email": user.email, "role": user.role},
        ip_address=request.client.host if request.client else None,
    )

    return {"message": "Account created. You may now sign in."}


@router.get("/mfa/setup", response_model=MFASetupResponse)
async def mfa_setup(
    user: Annotated[User, Depends(RequireEngineer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MFASetupResponse:
    if user.mfa_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is already enrolled.")

    if not user.mfa_secret:
        user.mfa_secret = pyotp.random_base32()
        await db.commit()

    uri = pyotp.TOTP(user.mfa_secret).provisioning_uri(
        name=user.email,
        issuer_name="PipelineGPT",
    )
    return MFASetupResponse(provisioning_uri=uri, secret=user.mfa_secret)


@router.post("/mfa/verify", status_code=status.HTTP_200_OK)
async def mfa_verify(
    body: MFAVerifyRequest,
    user: Annotated[User, Depends(RequireEngineer)],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    if user.mfa_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is already enrolled.")

    if not user.mfa_secret:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MFA setup not initiated. Call /auth/mfa/setup first.")

    totp = pyotp.TOTP(user.mfa_secret)
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code. Try again.")

    user.mfa_enabled = True
    await db.commit()

    await audit_log.log_event(
        db,
        event_type="USER_MFA_ENROLLED",
        actor_id=str(user.id),
        target_id=str(user.id),
        target_type="user",
        payload={"email": user.email},
        ip_address=request.client.host if request.client else None,
    )

    return {"message": "MFA enrolled successfully."}
