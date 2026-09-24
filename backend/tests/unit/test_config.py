from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

STRONG = "x" * 40


def test_production_rejects_default_jwt_secret():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(environment="production", jwt_secret="change-me-in-production", anthropic_api_key="k")


def test_production_rejects_short_jwt_secret():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(environment="production", jwt_secret="too-short", anthropic_api_key="k")


def test_production_requires_anthropic_key():
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings(environment="production", jwt_secret=STRONG, anthropic_api_key="")


def test_production_accepts_strong_config():
    s = Settings(environment="production", jwt_secret=STRONG, anthropic_api_key="k")
    assert s.is_production


def test_development_allows_defaults():
    assert Settings(environment="development").jwt_secret


def test_cors_origins_from_comma_separated_env(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example")
    assert Settings().cors_origins == ["https://a.example", "https://b.example"]
