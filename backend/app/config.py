from typing import Annotated, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_INSECURE_JWT_SECRETS = {"", "change-me-in-production", "secret", "changeme"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Set by the Docker image at build time (release tag).
    app_version: str = "0.2.0-dev"

    # "production" turns on fail-fast secret checks, HSTS, and hides /docs.
    environment: Literal["development", "test", "production"] = "development"
    # Demo deployments only: allows the seeded demo accounts and their pre-enrolled MFA bypass.
    demo_mode: bool = False

    database_url: str = "postgresql+asyncpg://pipelinegpt:pipelinegpt@localhost:5432/pipelinegpt"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str = ""

    jwt_secret: str = "change-me-in-production"  # noqa: S105 - placeholder, rejected in production
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 480  # 8 hours — one working shift
    mfa_pending_token_expire_minutes: int = 15

    # Comma-separated in env: CORS_ORIGINS=https://app.example.com,https://www.example.com
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://pipelinegpt.xyz",
        "https://www.pipelinegpt.xyz",
    ]

    # Brute-force protection: failed logins allowed per email (and per IP, x4) per window.
    login_max_failures: int = 5
    login_lockout_seconds: int = 900

    llm_model: str = "claude-sonnet-4-6"
    # spaCy model Presidio uses for PII detection (bundled in the Docker image).
    pii_spacy_model: str = "en_core_web_sm"

    max_tokens_per_day: int = 100_000
    upload_max_bytes: int = 52_428_800  # 50 MB

    hitl_confidence_threshold: float = 0.75
    top_k_retrieval: int = 12
    top_k_rerank: int = 6

    semantic_cache_similarity: float = 0.97
    semantic_cache_ttl_seconds: int = 86_400

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.environment == "production":
            if self.jwt_secret in _INSECURE_JWT_SECRETS or len(self.jwt_secret) < 32:
                raise ValueError(
                    "JWT_SECRET must be set to a random value of at least 32 characters in production "
                    '(e.g. python -c "import secrets; print(secrets.token_urlsafe(48))")'
                )
            if not self.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY must be set in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()
