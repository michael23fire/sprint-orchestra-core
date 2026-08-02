"""Contextual retrieval — Anthropic's technique (https://www.anthropic.com/news/contextual-retrieval).

A chunk read in isolation often loses the context that made it findable: "raising the pool size and
lowering max-lifetime resolved it" means nothing without knowing which pool, which incident, which
system. Before a multi-chunk source is embedded, this asks a small/fast LLM to write one short
sentence situating each chunk within its source document, and prepends that sentence to the chunk —
so the vector embedding, the lexical (tsvector) index, and the stored citation text all benefit from
the added context, not just the vector.

Only applied to sources that produce more than one chunk (see pipeline.py) — a single-chunk issue or
comment already fits entirely in its own "context," so contextualizing it would just pay for an LLM
call to restate what's already there. It earns its keep on long attachments, where a chunk pulled
from deep in a PDF has no idea what document or section it came from.

Off by default (VEC_CONTEXTUAL_RETRIEVAL_ENABLED=false) — this is a real LLM call per chunk, and a
noticeable ingestion cost/latency increase is a deliberate tradeoff, not something to switch on
silently.
"""
from __future__ import annotations

import logging
from typing import Protocol

import anthropic
import httpx

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """<document>
{document}
</document>
Here is the chunk we want to situate within the whole document:
<chunk>
{chunk}
</chunk>
Give a short, succinct sentence of context (no more than 2 sentences) that situates this chunk \
within the overall document, for the purpose of improving search retrieval of the chunk. Answer \
only with the context sentence — no preamble, no quotes, nothing else."""


class ContextGenerator(Protocol):
    async def generate(self, document: str, chunk: str) -> str: ...

    async def aclose(self) -> None: ...


class NoopContextGenerator:
    """Used when contextual retrieval is disabled — never calls out, always returns ''."""

    async def generate(self, document: str, chunk: str) -> str:
        return ""

    async def aclose(self) -> None:
        return None


class AnthropicContextGenerator:
    def __init__(self, api_key: str, model: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key or None)
        self._model = model

    async def generate(self, document: str, chunk: str) -> str:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=100,
            messages=[{"role": "user", "content": _PROMPT_TEMPLATE.format(document=document, chunk=chunk)}],
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()

    async def aclose(self) -> None:
        await self._client.close()


class OpenAICompatibleContextGenerator:
    """Local-testing stand-in — an OpenAI-compatible chat endpoint (e.g. LM Studio). Not production."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}, timeout=60.0
        )

    async def generate(self, document: str, chunk: str) -> str:
        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": _PROMPT_TEMPLATE.format(document=document, chunk=chunk)}],
            },
        )
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()

    async def aclose(self) -> None:
        await self._client.aclose()


def build_context_generator(settings) -> ContextGenerator:
    if not settings.contextual_retrieval_enabled:
        return NoopContextGenerator()
    if settings.contextual_llm_provider == "anthropic":
        return AnthropicContextGenerator(settings.contextual_anthropic_api_key, settings.contextual_anthropic_model)
    if settings.contextual_llm_provider == "openai_compatible":
        return OpenAICompatibleContextGenerator(
            settings.contextual_openai_base_url, settings.contextual_openai_api_key, settings.contextual_openai_model
        )
    raise ValueError(f"unknown contextual_llm_provider: {settings.contextual_llm_provider}")
