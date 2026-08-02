"""Thin client for vectorization-service's POST /embed — the one primitive the semantic cache needs.

Deliberately reuses vectorization-service's existing embedder rather than giving ai-service its own
embedding provider/credentials: same "index owner exposes primitives, callers compose them" split as
RetrievalClient (app/agent/retrieval_tool.py).
"""
from __future__ import annotations

from typing import List

import httpx

from app.observability import get_request_id


class EmbeddingClient:
    def __init__(self, base_url: str):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0)

    async def embed(self, text: str) -> List[float]:
        resp = await self._client.post(
            "/embed", json={"text": text}, headers={"x-request-id": get_request_id()}
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    async def aclose(self) -> None:
        await self._client.aclose()
