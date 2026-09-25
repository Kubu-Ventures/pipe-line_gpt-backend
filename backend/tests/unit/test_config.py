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


def test_production_with_cloud_provider_needs_no_anthropic_key():
    s = Settings(
        environment="production",
        jwt_secret=STRONG,
        llm_provider="bedrock",
        aws_region="us-east-1",
        llm_model="anthropic.claude-sonnet-5",
    )
    assert s.llm_provider == "bedrock"


def test_bedrock_requires_region():
    with pytest.raises(ValidationError, match="AWS_REGION"):
        Settings(llm_provider="bedrock", aws_region="", llm_model="anthropic.claude-sonnet-5")


def test_bedrock_requires_prefixed_model_id():
    with pytest.raises(ValidationError, match="anthropic.claude-sonnet-5"):
        Settings(llm_provider="bedrock", aws_region="us-east-1", llm_model="claude-sonnet-4-6")


@pytest.mark.parametrize("provider", ["anthropic", "vertex"])
def test_bedrock_model_id_rejected_for_other_providers(provider):
    with pytest.raises(ValidationError, match="Bedrock model ID"):
        Settings(llm_provider=provider, vertex_project_id="p", llm_model="anthropic.claude-sonnet-5")


def test_vertex_requires_project():
    with pytest.raises(ValidationError, match="VERTEX_PROJECT_ID"):
        Settings(llm_provider="vertex", vertex_project_id="")


@pytest.mark.parametrize("langs", ["eng", "eng+spa", "chi_sim+eng"])
def test_ocr_languages_accepted(langs):
    assert Settings(jwt_secret=STRONG, ocr_languages=langs).ocr_languages == langs


@pytest.mark.parametrize("langs", ["", "eng spa", "eng;rm", "--psm"])
def test_ocr_languages_rejects_anything_but_tesseract_codes(langs):
    with pytest.raises(ValidationError):
        Settings(jwt_secret=STRONG, ocr_languages=langs)
