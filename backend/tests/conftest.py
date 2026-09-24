"""Shared pytest configuration.

Unit tests (tests/unit) run with no external services.

Integration tests (tests/integration) need Postgres+pgvector and Redis and are skipped
unless TEST_DATABASE_URL is set. The database it points at is DROPPED and re-migrated
at the start of the session, so it must be a dedicated test database (its name must
contain "test").
"""

from __future__ import annotations

import os

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

# Must run before anything imports app.config — env vars take precedence over .env.
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
os.environ["JWT_SECRET"] = "test-jwt-secret-not-for-production"
# Never let the suite reach the real Anthropic API; LLM calls are patched in tests.
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    for item in items:
        if "tests/integration/" in item.nodeid:
            item.add_marker(pytest.mark.integration)
            if not TEST_DATABASE_URL:
                item.add_marker(skip)
