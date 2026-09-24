from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.middleware.auth import (
    create_access_token,
    get_current_user,
    get_db,
    get_user_allow_mfa_setup,
    hash_password,
    verify_password,
)
from app.middleware.rate_limit import (
    claim_totp_code,
    clear_login_failures,
    ensure_login_allowed,
    record_login_failure,
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

MFA_REQUIRED_ROLES = ("ENGINEER", "ADMIN")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _is_enrolled(user: User) -> bool:
    return bool(user.mfa_enabled and user.mfa_secret)


def _verify_totp(user: User, code: str) -> bool:
    return bool(user.mfa_secret) and pyotp.TOTP(user.mfa_secret).verify(code, valid_window=1)


async def _reject_login(db: AsyncSession, request: Request, email: str, reason: str, detail: object) -> None:
    ip = _client_ip(request)
    await record_login_failure(email, ip)
    await audit_log.log_event(
        db,
        event_type="USER_LOGIN_FAILED",
        target_id=email,
        target_type="user",
        payload={"email": email, "reason": reason},
        ip_address=ip,
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    email = body.email.lower()
    await ensure_login_allowed(email, _client_ip(request))

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not verify_password(body.password, user.hashed_password if user else None):
        await _reject_login(db, request, email, "bad_credentials", "Invalid credentials")

    if user.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account suspended. Contact your administrator.",
        )

    scope = "full"
    mfa_setup_required = False
    if _is_enrolled(user):
        if not body.totp_code:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "mfa_required", "message": "Enter the 6-digit code from your authenticator app."},
            )
        if not _verify_totp(user, body.totp_code) or not await claim_totp_code(str(user.id), body.totp_code):
            await _reject_login(
                db,
                request,
                email,
                "bad_totp",
                {"code": "mfa_invalid", "message": "Invalid or already-used authentication code."},
            )
    elif user.role in MFA_REQUIRED_ROLES and not (settings.demo_mode and user.mfa_enabled):
        # Not enrolled (demo accounts are pre-flagged and skip this only in DEMO_MODE):
        # issue a token that can only reach the enrollment endpoints.
        scope = "mfa_setup"
        mfa_setup_required = True

    await clear_login_failures(email)
    user.last_login = datetime.now(UTC)
    await db.commit()

    await audit_log.log_event(
        db,
        event_type="USER_LOGIN",
        actor_id=str(user.id),
        target_id=str(user.id),
        target_type="user",
        payload={"email": user.email, "mfa_setup_required": mfa_setup_required},
        ip_address=_client_ip(request),
    )

    return TokenResponse(
        access_token=create_access_token(str(user.id), user.role, scope=scope),
        mfa_setup_required=mfa_setup_required,
    )


@router.get("/me", response_model=UserOut)
async def get_me(user: Annotated[User, Depends(get_user_allow_mfa_setup)]) -> UserOut:
    return UserOut.model_validate(user)


_SUPPORTED_LOCALES = {"en", "fr", "es", "ar", "zh", "ru", "pt", "de", "ja", "hi"}


@router.patch("/me/language", response_model=UserOut)
async def update_preferred_language(
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    lang = str(body.get("language", "")).lower()
    if lang not in _SUPPORTED_LOCALES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported language. Must be one of: {sorted(_SUPPORTED_LOCALES)}",
        )
    user.preferred_lang = lang
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/accept-invite", status_code=status.HTTP_201_CREATED)
async def accept_invite(
    body: AcceptInviteRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    now = datetime.now(UTC)

    result = await db.execute(select(Invitation).where(Invitation.token == body.token))
    invite = result.scalar_one_or_none()

    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found or already used.")

    if invite.accepted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This invitation has already been accepted.")

    expires = invite.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if now > expires:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Invitation has expired. Request a new one from your administrator.",
        )

    existing = await db.execute(select(User).where(User.email == invite.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.")

    user = User(
        id=uuid.uuid4(),
        email=invite.email.lower(),
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
        payload={"email": invite.email, "role": invite.role},
        ip_address=_client_ip(request),
    )

    return {"message": "Account created. You may now sign in."}


@router.get("/mfa/setup", response_model=MFASetupResponse)
async def mfa_setup(
    user: Annotated[User, Depends(get_user_allow_mfa_setup)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MFASetupResponse:
    if user.role not in MFA_REQUIRED_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
    if _is_enrolled(user):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is already enrolled.")

    if not user.mfa_secret:
        user.mfa_secret = pyotp.random_base32()
        # Legacy rows were flagged enabled without a secret; enrollment starts over.
        user.mfa_enabled = False
        await db.commit()

    uri = pyotp.TOTP(user.mfa_secret).provisioning_uri(name=user.email, issuer_name="PipelineGPT")
    return MFASetupResponse(provisioning_uri=uri, secret=user.mfa_secret)


@router.post("/mfa/verify", status_code=status.HTTP_200_OK)
async def mfa_verify(
    body: MFAVerifyRequest,
    user: Annotated[User, Depends(get_user_allow_mfa_setup)],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    if user.role not in MFA_REQUIRED_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
    if _is_enrolled(user):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is already enrolled.")
    if not user.mfa_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup not initiated. Call /auth/mfa/setup first.",
        )

    if not _verify_totp(user, body.code) or not await claim_totp_code(str(user.id), body.code):
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
        ip_address=_client_ip(request),
    )

    return {"message": "MFA enrolled successfully. Sign in again with your authentication code."}
