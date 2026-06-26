from __future__ import annotations

import hashlib

import httpx

from app.config import settings


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts using OpenAI text-embedding-3-small."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={"model": settings.embedding_model, "input": texts},
        )
        response.raise_for_status()
    data = response.json()
    # Sort by index to maintain order
    items = sorted(data["data"], key=lambda x: x["index"])
    return [item["embedding"] for item in items]


async def embed_single(text: str) -> list[float]:
    embeddings = await embed_texts([text])
    return embeddings[0]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
