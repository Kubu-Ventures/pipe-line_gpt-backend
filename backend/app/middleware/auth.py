from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db import User

bearer_scheme = HTTPBearer(auto_error=False)

# "full": normal session. "mfa_setup": issued to ENGINEER/ADMIN users who have not
# enrolled TOTP yet; only accepted by the MFA enrollment endpoints and /auth/me.
TokenScope = Literal["full", "mfa_setup"]

# Hash compared against when the email is unknown, so login timing doesn't reveal
# which accounts exist.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt(12)).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), (hashed or _DUMMY_HASH).encode("utf-8")) and hashed is not None


def create_access_token(sub: str, role: str, scope: TokenScope = "full") -> str:
    now = datetime.now(UTC)
    minutes = settings.access_token_expire_minutes if scope == "full" else settings.mfa_pending_token_expire_minutes
    payload = {
        "sub": sub,
        "role": role,
        "scope": scope,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise _unauthorized("Invalid or expired token") from exc


async def get_db() -> AsyncSession:
    from app.main import async_session_factory

    async with async_session_factory() as session:
        yield session


async def _resolve_user(
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
    allowed_scopes: tuple[TokenScope, ...],
) -> User:
    if not credentials:
        raise _unauthorized("Not authenticated")

    payload = decode_token(credentials.credentials)
    if payload.get("scope") not in allowed_scopes:
        if payload.get("scope") == "mfa_setup":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MFA enrollment required")
        raise _unauthorized("Invalid or expired token")

    try:
        user_id = uuid.UUID(payload.get("sub", ""))
    except ValueError as exc:
        raise _unauthorized("Invalid or expired token") from exc

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise _unauthorized("User not found")
    if user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active")
    return user


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    return await _resolve_user(credentials, db, ("full",))


async def get_user_allow_mfa_setup(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """For the endpoints a not-yet-enrolled engineer/admin must reach: /auth/me and MFA enrollment."""
    return await _resolve_user(credentials, db, ("full", "mfa_setup"))


def require_role(*roles: str):
    async def _check(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return _check


RequireOperator = require_role("OPERATOR", "ENGINEER", "ADMIN")
RequireEngineer = require_role("ENGINEER", "ADMIN")
RequireAdmin = require_role("ADMIN")
