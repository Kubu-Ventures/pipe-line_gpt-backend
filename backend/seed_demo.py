"""
Seed demo accounts for PipelineGPT.

Usage:
    python seed_demo.py

Idempotent — safe to run multiple times. Skips accounts that already exist.

Demo credentials:
    demo-operator@pipelinegpt.xyz / DemoOp2026!   (OPERATOR)
    demo-engineer@pipelinegpt.xyz / DemoEng2026!  (ENGINEER, MFA pre-enrolled)
    demo-admin@pipelinegpt.xyz    / DemoAdmin2026! (ADMIN,    MFA pre-enrolled)
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.middleware.auth import hash_password as _hash

DEMO_ACCOUNTS = [
    {
        "email": "demo-operator@pipelinegpt.xyz",
        "password": "DemoOp2026!",
        "role": "OPERATOR",
        "mfa_enabled": False,
    },
    {
        "email": "demo-engineer@pipelinegpt.xyz",
        "password": "DemoEng2026!",
        "role": "ENGINEER",
        "mfa_enabled": True,
    },
    {
        "email": "demo-admin@pipelinegpt.xyz",
        "password": "DemoAdmin2026!",
        "role": "ADMIN",
        "mfa_enabled": True,
    },
]


async def seed() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Import here to avoid circular imports when run standalone
    from app.models.db import User  # noqa: PLC0415

    async with factory() as session:
        for acc in DEMO_ACCOUNTS:
            result = await session.execute(select(User).where(User.email == acc["email"]))
            existing = result.scalar_one_or_none()
            if existing:
                # Always sync mfa_enabled so removing MFA from demo accounts takes effect
                if existing.mfa_enabled != acc["mfa_enabled"]:
                    existing.mfa_enabled = acc["mfa_enabled"]
                    print(f"  UPDATE {acc['email']} (mfa_enabled → {acc['mfa_enabled']})")
                else:
                    print(f"  SKIP  {acc['email']} (already exists)")
                continue

            user = User(
                id=uuid.uuid4(),
                email=acc["email"],
                hashed_password=_hash(acc["password"]),
                role=acc["role"],
                status="ACTIVE",
                mfa_enabled=acc["mfa_enabled"],
            )
            session.add(user)
            print(f"  CREATE {acc['email']} ({acc['role']})")

        await session.commit()

    await engine.dispose()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(seed())
