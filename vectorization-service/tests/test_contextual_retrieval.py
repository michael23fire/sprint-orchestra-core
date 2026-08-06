"""Contextual retrieval wiring — fake context generator, no LLM/network needed.

Covers: disabled by default (no-op), applied only to multi-chunk sources (single-chunk sources skip
the LLM call entirely — see pipeline.py's cost-optimization comment), and a generation failure
degrades gracefully instead of failing ingestion.
"""
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.ingest.embedder import FakeEmbedder
from app.ingest.pipeline import IngestPipeline
from app.models import IssueIngestionMessage


class RecordingContextGenerator:
    """Returns a fixed context string and records every (document, chunk) it was asked about."""

    def __init__(self, context: str = "SITUATING CONTEXT"):
        self._context = context
        self.calls = []

    async def generate(self, document: str, chunk: str) -> str:
        self.calls.append((document, chunk))
        return self._context

    async def aclose(self) -> None:
        return None


class FailingContextGenerator:
    async def generate(self, document: str, chunk: str) -> str:
        raise RuntimeError("LLM API down")

    async def aclose(self) -> None:
        return None


class InMemoryStore:
    def __init__(self):
        self.rows = {}
        self.issues = {}

    async def replace_source(self, chunk_type, source_id, chunks, vectors):
        self.rows = {cid: c for cid, c in self.rows.items() if not (c.chunk_type == chunk_type and c.source_id == source_id)}
        for c in chunks:
            self.rows[c.id] = c

    async def delete_source(self, chunk_type, source_id) -> int:
        before = len(self.rows)
        self.rows = {cid: c for cid, c in self.rows.items() if not (c.chunk_type == chunk_type and c.source_id == source_id)}
        return before - len(self.rows)

    async def upsert_issue(self, meta) -> None:
        self.issues[meta.issue_id] = meta

    async def delete_issue(self, issue_id) -> int:
        before = len(self.rows)
        self.rows = {cid: c for cid, c in self.rows.items() if c.issue_id != issue_id}
        self.issues.pop(issue_id, None)
        return before - len(self.rows)


def _issue_msg(description: str) -> IssueIngestionMessage:
    return IssueIngestionMessage(
        eventId="e1", eventType="issue_upserted", emittedAt=datetime.now(timezone.utc),
        issueId=1, issueKey="X-1", spaceId=7, title="t", description=description,
    )


def _pipeline(context_generator, enabled: bool, chunk_size: int = 500):
    settings = Settings(
        embedding_provider="fake", embedding_dim=8,
        contextual_retrieval_enabled=enabled,
        chunk_size_tokens=chunk_size, chunk_overlap_tokens=10,
    )
    store = InMemoryStore()
    pipeline = IngestPipeline(settings, FakeEmbedder(8), store, context_generator)
    return pipeline, store


async def test_disabled_by_default_never_calls_context_generator():
    gen = RecordingContextGenerator()
    pipeline, store = _pipeline(gen, enabled=False)
    await pipeline.handle_issue(_issue_msg("short description"))
    assert gen.calls == []


async def test_single_chunk_source_skips_context_generation():
    # Contextualizing a source that's already one chunk would just restate what's already there —
    # skip the LLM call entirely (see pipeline.py _chunks).
    gen = RecordingContextGenerator()
    pipeline, store = _pipeline(gen, enabled=True)
    await pipeline.handle_issue(_issue_msg("a short description that fits in one chunk"))
    assert gen.calls == []


async def test_multi_chunk_source_gets_contextualized():
    long_text = " ".join(f"word{i}" for i in range(1200))  # forces multiple chunks at size 500
    gen = RecordingContextGenerator("This chunk discusses word usage patterns.")
    pipeline, store = _pipeline(gen, enabled=True, chunk_size=500)

    await pipeline.handle_issue(_issue_msg(long_text))

    assert len(gen.calls) > 1  # one call per chunk
    # Every chunk carries the contextual sentence. Chunk #0 starts with it directly — handle_issue
    # prepends the issue's own key into the text before chunking, so chunk #0 already self-identifies
    # and doesn't get the deterministic "[Issue X-1]" tag (see pipeline.py's _chunks). Chunk #1+ has
    # lost that natural prefix past the split point, so it gets tagged ahead of the contextual
    # sentence instead (see test_pipeline_idempotency.py's tagging tests for the isolated case).
    chunks = sorted(store.rows.values(), key=lambda c: c.chunk_index)
    assert chunks[0].content.startswith("This chunk discusses word usage patterns.\n\n")
    assert all(
        c.content.startswith("[Issue X-1]\nThis chunk discusses word usage patterns.\n\n")
        for c in chunks[1:]
    )


async def test_context_generation_failure_falls_back_to_plain_chunk():
    long_text = " ".join(f"word{i}" for i in range(1200))
    pipeline, store = _pipeline(FailingContextGenerator(), enabled=True, chunk_size=500)

    # Must not raise — a down context-generation LLM must not break ingestion.
    await pipeline.handle_issue(_issue_msg(long_text))

    assert len(store.rows) > 1
    assert all("word0" in c.content or "word" in c.content for c in store.rows.values())
    assert not any(c.content.startswith("This chunk") for c in store.rows.values())
