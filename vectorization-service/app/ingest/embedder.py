"""Embedding providers.

Anthropic does not offer an embeddings API — Claude is a generation model. For RAG embeddings
Anthropic recommends **Voyage AI**, so that is the default here; **OpenAI** is supported as an
alternative, and a **fake** deterministic embedder lets the service (and its tests) run with no API
key or network.

All providers expose the same async ``embed(texts) -> list[vector]`` surface, batch internally, and
retry transient failures. Embedding calls are the slow, failure-prone part of ingestion — which is
exactly why this work lives behind Kafka in its own service rather than inline in jira-backend.
"""
from __future__ import annotations

import hashlib
import logging
from typing import List, Protocol

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import Settings

logger = logging.getLogger(__name__)

Vector = List[float]


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: List[str]) -> List[Vector]: ...

    async def aclose(self) -> None: ...


def _batched(items: List[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


class _HttpEmbedder:
    """Shared batching + retry wrapper for REST-based providers (Voyage, OpenAI)."""

    endpoint: str
    dim: int

    def __init__(self, settings: Settings, api_key: str, endpoint: str | None = None):
        self._model = settings.embedding_model
        self._batch_size = settings.embedding_batch_size
        self._max_retries = settings.embedding_max_retries
        self.dim = settings.embedding_dim
        if endpoint is not None:
            self.endpoint = endpoint
        self._client = httpx.AsyncClient(
            timeout=60.0,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    async def embed(self, texts: List[str]) -> List[Vector]:
        vectors: List[Vector] = []
        for batch in _batched(texts, self._batch_size):
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, batch: List[str]) -> List[Vector]:
        # tenacity retries transient API failures (429/5xx/timeouts) with backoff. A batch that
        # still fails after retries raises, so the Kafka consumer does not commit its offset and the
        # message is redelivered — at-least-once, which the deterministic upsert makes safe.
        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            reraise=True,
        )
        async def _call() -> List[Vector]:
            resp = await self._client.post(self.endpoint, json=self._payload(batch))
            resp.raise_for_status()
            return self._parse(resp.json())

        return await _call()

    def _payload(self, batch: List[str]) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    def _parse(self, data: dict) -> List[Vector]:
        return [item["embedding"] for item in data["data"]]

    async def aclose(self) -> None:
        await self._client.aclose()


class VoyageEmbedder(_HttpEmbedder):
    endpoint = "https://api.voyageai.com/v1/embeddings"

    def _payload(self, batch: List[str]) -> dict:
        # input_type="document" tells Voyage these are corpus docs (queries use "query" at search
        # time in the AI service) — it tunes the embedding space for retrieval.
        return {"model": self._model, "input": batch, "input_type": "document"}


class OpenAIEmbedder(_HttpEmbedder):
    """OpenAI or any OpenAI-compatible server (LM Studio / Ollama / vLLM), via ``openai_base_url``."""

    def _payload(self, batch: List[str]) -> dict:
        return {"model": self._model, "input": batch}


class FakeEmbedder:
    """Deterministic hash-based vectors — no network, no key. For local runs and tests only."""

    def __init__(self, dim: int):
        self.dim = dim

    async def embed(self, texts: List[str]) -> List[Vector]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> Vector:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        # Cycle the digest bytes into `dim` floats in [0, 1). Deterministic: same text -> same vector.
        return [seed[i % len(seed)] / 255.0 for i in range(self.dim)]

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        return None


def build_embedder(settings: Settings) -> Embedder:
    provider = settings.embedding_provider.lower()
    if provider == "voyage":
        if not settings.voyage_api_key:
            logger.warning("VEC_VOYAGE_API_KEY is empty; falling back to the fake embedder")
            return FakeEmbedder(settings.embedding_dim)
        return VoyageEmbedder(settings, settings.voyage_api_key)
    if provider == "openai":
        base_url = settings.openai_base_url.rstrip("/")
        is_local = "api.openai.com" not in base_url
        # A local OpenAI-compatible server (LM Studio/Ollama) needs no real key — any string works.
        if not settings.openai_api_key and not is_local:
            logger.warning("VEC_OPENAI_API_KEY is empty; falling back to the fake embedder")
            return FakeEmbedder(settings.embedding_dim)
        api_key = settings.openai_api_key or "local"
        return OpenAIEmbedder(settings, api_key, endpoint=f"{base_url}/embeddings")
    if provider == "fake":
        return FakeEmbedder(settings.embedding_dim)
    raise ValueError(f"unknown embedding_provider: {settings.embedding_provider}")
