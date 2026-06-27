"""
CLI script to create the first admin account.

Usage:
    python create_admin.py <email> <password>

The admin is created with status=ACTIVE and mfa_enabled=True (pre-enrolled).
Subsequent admins should be created through the /admin/invite endpoint.
"""
from __future__ import annotations

import asyncio
import sys
import uuid

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = "postgresql+asyncpg://pipelinegpt:pipelinegpt@localhost:5432/pipelinegpt"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def create_admin(email: str, password: str) -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.models.db import User  # noqa: PLC0415

    async with factory() as session:
        result = await session.execute(select(User).where(User.email == email.lower()))
        if result.scalar_one_or_none():
            print(f"Error: a user with email '{email}' already exists.")
            await engine.dispose()
            sys.exit(1)

        user = User(
            id=uuid.uuid4(),
            email=email.lower(),
            hashed_password=pwd_context.hash(password),
            role="ADMIN",
            status="ACTIVE",
            mfa_enabled=True,
        )
        session.add(user)
        await session.commit()
        print(f"Admin account created: {email}")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python create_admin.py <email> <password>")
        sys.exit(1)

    email_arg = sys.argv[1]
    password_arg = sys.argv[2]

    if len(password_arg) < 8:
        print("Error: password must be at least 8 characters.")
        sys.exit(1)

    asyncio.run(create_admin(email_arg, password_arg))
