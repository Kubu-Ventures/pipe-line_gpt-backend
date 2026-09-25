"""
CLI script to create the first admin account.

Usage:
    python create_admin.py <email>                     # prompts for the password
    python create_admin.py <email> --password-stdin    # reads it from stdin (for scripts)

The admin is created ACTIVE without MFA; the first sign-in forces TOTP enrollment.
Subsequent admins should be created through the /admin/invite endpoint.
"""

from __future__ import annotations

import argparse
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


def read_password(from_stdin: bool) -> str:
    """Return a validated password, or raise SystemExit with a message."""
    if from_stdin:
        # One line, so a script can pipe it in: printf '%s\n' "$pw" | ... --password-stdin
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password (min 12 characters): ")
    if len(password) < 12:
        raise SystemExit("Error: password must be at least 12 characters.")
    if not from_stdin and getpass.getpass("Confirm password: ") != password:
        raise SystemExit("Error: passwords do not match.")
    return password


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create an ADMIN account.")
    parser.add_argument("email")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from the first line of stdin instead of prompting",
    )
    args = parser.parse_args(argv)
    password = read_password(args.password_stdin)
    asyncio.run(create_admin(args.email, password))


if __name__ == "__main__":
    main()
