"""
CLI script to verify the configured AI provider answers, before operators rely on it.

Usage:
    python check_llm.py          # or, in the image: docker compose run --rm api llm-check

Sends one tiny request with the current LLM_PROVIDER / LLM_MODEL settings and prints
"OK" or a plain-English reason. Exit code 0 on success, 1 on failure.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.services.llm import make_client, user_facing_llm_error


def _target() -> str:
    if settings.llm_provider == "bedrock":
        return f"bedrock ({settings.aws_region})"
    if settings.llm_provider == "vertex":
        return f"vertex ({settings.vertex_project_id}, {settings.vertex_region})"
    return "anthropic"


async def check() -> int:
    try:
        async with make_client() as client:
            await client.messages.create(
                model=settings.llm_model,
                max_tokens=1024,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            )
    except Exception as exc:
        print(f"FAILED: {_target()}, model {settings.llm_model}")
        print(f"  {user_facing_llm_error(exc)}")
        # The raw error names the missing permission, region or model; operators need it here.
        print(f"  Details: {type(exc).__name__}: {exc}")
        return 1
    print(f"OK: {_target()}, model {settings.llm_model}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(check()))
