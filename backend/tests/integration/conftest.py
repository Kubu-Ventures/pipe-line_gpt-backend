from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.integration.helpers import DEFAULT_PASSWORD

BACKEND_DIR = Path(__file__).resolve().parents[2]

# Child tables first so plain DELETEs satisfy FKs. DELETE is much cheaper than
# TRUNCATE for the handful of rows a test creates.
TABLES = (
    "audit_events",
    "hitl_reviews",
    "responses",
    "queries",
    "chunks",
    "documents",
    "invitations",
    "users",
)


async def _reset_schema(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    url = os.environ["TEST_DATABASE_URL"]
    if "test" not in (make_url(url).database or ""):
        pytest.exit(f"Refusing to wipe non-test database: {make_url(url).database!r}", returncode=2)

    asyncio.run(_reset_schema(url))
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
async def _clean_state() -> AsyncIterator[None]:
    yield
    from app.main import engine
    from app.middleware.rate_limit import get_redis

    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - fixed table names
    await get_redis().flushdb()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    from app.main import async_session_factory

    async with async_session_factory() as session:
        yield session


@pytest.fixture
def make_user(db_session: AsyncSession) -> Callable[..., Awaitable[object]]:
    from app.middleware.auth import hash_password
    from app.models.db import User

    async def _make(
        role: str = "OPERATOR",
        *,
        email: str | None = None,
        password: str = DEFAULT_PASSWORD,
        status: str = "ACTIVE",
        mfa_enabled: bool = False,
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            email=email or f"{role.lower()}-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password=hash_password(password),
            role=role,
            status=status,
            mfa_enabled=mfa_enabled,
        )
        db_session.add(user)
        await db_session.commit()
        return user

    return _make
