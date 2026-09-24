"""
CLI script to create the first admin account.

Usage:
    python create_admin.py <email>          # prompts for the password

The admin is created ACTIVE without MFA; the first sign-in forces TOTP enrollment.
Subsequent admins should be created through the /admin/invite endpoint.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.middleware.auth import hash_password


async def create_admin(email: str, password: str) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
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
            hashed_password=hash_password(password),
            role="ADMIN",
            status="ACTIVE",
            mfa_enabled=False,
        )
        session.add(user)
        await session.commit()
        print(f"Admin account created: {email}")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python create_admin.py <email>")
        sys.exit(1)

    email_arg = sys.argv[1]
    password_arg = getpass.getpass("Password (min 12 characters): ")
    if len(password_arg) < 12:
        print("Error: password must be at least 12 characters.")
        sys.exit(1)
    if getpass.getpass("Confirm password: ") != password_arg:
        print("Error: passwords do not match.")
        sys.exit(1)

    asyncio.run(create_admin(email_arg, password_arg))
