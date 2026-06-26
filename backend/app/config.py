from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://pipelinegpt:pipelinegpt@localhost:5432/pipelinegpt"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str = ""
    openai_api_key: str = ""

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    embedding_model: str = "text-embedding-3-small"
    llm_model: str = "claude-sonnet-4-6"

    max_tokens_per_day: int = 100_000
    upload_max_bytes: int = 52_428_800  # 50 MB

    hitl_confidence_threshold: float = 0.75
    top_k_retrieval: int = 12
    top_k_rerank: int = 6

    semantic_cache_similarity: float = 0.97
    semantic_cache_ttl_seconds: int = 86_400


settings = Settings()
