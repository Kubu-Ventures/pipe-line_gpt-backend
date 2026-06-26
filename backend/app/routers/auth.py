from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.auth import (
    RequireAdmin,
    create_access_token,
    get_current_user,
    get_db,
    hash_password,
    verify_password,
)
from app.models.db import User
from app.models.schemas import LoginRequest, TokenResponse, UserCreate, UserOut
from app.services import audit_log

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: UserCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        id=uuid.uuid4(),
        email=body.email,
        hashed_password=hash_password(body.password),
        role=body.role,
        preferred_lang=body.preferred_lang,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    await audit_log.log_event(
        db,
        event_type="USER_REGISTERED",
        actor_id=str(user.id),
        target_id=str(user.id),
        target_type="user",
        payload={"email": user.email, "role": user.role},
        ip_address=request.client.host if request.client else None,
    )

    return UserOut.model_validate(user)


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

    user.last_login = datetime.now(timezone.utc)
    await db.commit()

    token = create_access_token(str(user.id), user.role)

    await audit_log.log_event(
        db,
        event_type="USER_LOGIN",
        actor_id=str(user.id),
        target_id=str(user.id),
        target_type="user",
        payload={"email": user.email},
        ip_address=request.client.host if request.client else None,
    )

    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserOut)
async def get_me(user: Annotated[User, Depends(get_current_user)]) -> UserOut:
    return UserOut.model_validate(user)
