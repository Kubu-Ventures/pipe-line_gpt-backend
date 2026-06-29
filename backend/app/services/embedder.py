from __future__ import annotations

import asyncio
import hashlib
from functools import lru_cache

EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384


@lru_cache(maxsize=1)
def _get_model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=EMBED_MODEL_NAME)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts using fastembed (ONNX-based, no API key, no CUDA required)."""
    model = _get_model()
    loop = asyncio.get_event_loop()
    embeddings = await loop.run_in_executor(
        None, lambda: [e.tolist() for e in model.embed(texts)]
    )
    return embeddings


async def embed_single(text: str) -> list[float]:
    results = await embed_texts([text])
    return results[0]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
