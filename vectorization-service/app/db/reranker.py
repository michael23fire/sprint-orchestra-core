"""Second-stage reranking: a cross-encoder re-scores the RRF-fused candidate pool.

Why this exists on top of hybrid RRF fusion: RRF combines *ranks* from two retrievers that never
looked at query and chunk together — vector search scores a chunk against the query independently
of lexical search doing the same. A cross-encoder is different in kind: it takes (query, chunk) as
a single joint input and outputs one relevance score, which is strictly more informative than
combining two independent rankings, at the cost of being too slow to run over the whole index (hence
"first stage retrieves broadly and cheaply, second stage reranks a small candidate pool precisely" —
the standard two-stage retrieval architecture used in production search/RAG systems).

Off by default (``VEC_RERANK_ENABLED=false``): loading the cross-encoder model pulls in
``sentence-transformers`` (a torch dependency, ~500MB-1GB download) and adds real inference latency
per search (tens of ms per candidate on CPU) — a genuine latency/precision tradeoff, not something to
enable silently. See README "Reranking" section for measured before/after numbers.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Protocol

from app.models import SearchHit

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    async def rerank(self, query: str, hits: List[SearchHit], top_n: int) -> List[SearchHit]: ...


class NoopReranker:
    """Default: pass the first-stage ranking through unchanged, just truncated to top_n."""

    async def rerank(self, query: str, hits: List[SearchHit], top_n: int) -> List[SearchHit]:
        return hits[:top_n]


class CrossEncoderReranker:
    """Wraps a local ``sentence_transformers.CrossEncoder`` model.

    The model is loaded once, synchronously, at construction time (during app startup, not per
    request) — this is the ~1-2s "cold load" cost you pay once, not per search.
    ``CrossEncoder.predict`` is a blocking, CPU-bound call; running it directly inside an async
    request handler would stall the event loop for every other in-flight request, so it's pushed
    onto a worker thread via ``asyncio.to_thread``.
    """

    def __init__(self, model_name: str, score_threshold: float | None = None):
        from sentence_transformers import CrossEncoder  # imported lazily: heavy, torch-backed

        logger.info(
            "loading cross-encoder reranker model=%s score_threshold=%s", model_name, score_threshold
        )
        self._model = CrossEncoder(model_name)
        self._score_threshold = score_threshold

    async def rerank(self, query: str, hits: List[SearchHit], top_n: int) -> List[SearchHit]:
        if not hits:
            return hits
        # Prefix the issue key onto the scored text, not just `hit.content`: the key is never part
        # of `content` itself (app/ingest/pipeline.py stores chunk content and issue_key as separate
        # fields — see Chunk in app/models.py), so a cross-encoder scoring `content` alone has no way
        # to recognize an exact-identifier query (e.g. "ATLAS-6") as relevant to its own chunk, and
        # will rank it as low-relevance prose. Confirmed by eval: reranking such queries against
        # content-only pairs dropped lexical-slice MRR from 0.70 (hybrid, no rerank) to 0.25 even
        # though plain FTS (which does index the key — see migrations/003) found the right chunk.
        pairs = [(query, f"{hit.issue_key} {hit.content}") for hit in hits]
        scores = await asyncio.to_thread(self._model.predict, pairs)
        ranked = sorted(zip(hits, scores), key=lambda pair: pair[1], reverse=True)
        if self._score_threshold is not None:
            # Drop candidates the cross-encoder itself judged irrelevant, rather than always
            # returning `top_n` regardless of quality. Deliberately allowed to return an empty list:
            # the CRAG loop upstream (ai-service/app/agent/crag_loop.py) already treats "No results
            # found." as a signal to reformulate and search again, so a bad candidate pool should
            # surface as "nothing relevant" rather than be padded out with a top-1 guess.
            ranked = [pair for pair in ranked if pair[1] >= self._score_threshold]
        return [
            SearchHit(
                id=hit.id,
                chunk_type=hit.chunk_type,
                issue_id=hit.issue_id,
                issue_key=hit.issue_key,
                space_id=hit.space_id,
                source_id=hit.source_id,
                content=hit.content,
                score=float(score),
                retrievers=hit.retrievers,
                page_number=hit.page_number,
                provenance=hit.provenance,
            )
            for hit, score in ranked[:top_n]
        ]


def build_reranker(settings) -> Reranker:
    if not settings.rerank_enabled:
        return NoopReranker()
    return CrossEncoderReranker(settings.rerank_model, settings.rerank_score_threshold)
