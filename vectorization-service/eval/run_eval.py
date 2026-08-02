"""End-to-end retrieval evaluation.

Runs the *real* ingestion path (chunk -> embed -> pgvector) over the AtlasCart corpus, then queries
it through the **same production code** the ai-service tool call and ``POST /search`` use —
``VectorStore.search_vector`` / ``search_lexical`` / ``search_hybrid`` (app/db/vector_store.py) and
``CrossEncoderReranker`` (app/db/reranker.py) — instead of a hand-rolled query. That distinction
matters: an earlier version of this script ran its own raw SQL cosine query, so it only ever measured
the vector-only path and silently never exercised lexical fusion or reranking at all, even after both
shipped. Scoring each of the four modes below on the same labeled queries answers, with numbers, "does
hybrid actually help over vector-only, and does reranking help over hybrid" instead of asserting it.

Usage (with the local LM Studio embedding server running, or any OpenAI-compatible endpoint):

    VEC_EMBEDDING_PROVIDER=openai \
    VEC_OPENAI_BASE_URL=http://localhost:1234/v1 \
    VEC_EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b \
    VEC_EMBEDDING_DIM=1024 \
    VEC_PG_DSN=postgresql://vec:vec123@localhost:5433/vecdb \
    python -m eval.run_eval

Reranking needs no embedding provider or key of its own (it scores (query, chunk text) pairs
directly, not vectors) but does need ``sentence-transformers`` installed — it loads the same
``VEC_RERANK_MODEL`` production uses.

The query search lives here (not in the service) on purpose: this service owns the *write* side; the
read/query side belongs to the future AI service. This harness stands in for that reader.
"""
from __future__ import annotations

import asyncio
from typing import List, Set, Tuple

from app.config import Settings
from app.db.pool import create_pool
from app.db.reranker import CrossEncoderReranker
from app.db.vector_store import VectorStore
from app.ingest.embedder import build_embedder
from app.ingest.pipeline import IngestPipeline
from app.models import CommentIngestionMessage, IssueIngestionMessage, SearchHit
from eval.dataset import CORPUS, QUERIES
from eval.metrics import aggregate, hit_at_k

TOP_K = 10
KS = (1, 3, 5)
SPACE_IDS = [1]  # every corpus issue/comment in eval/dataset.py uses spaceId=1


async def _ingest(pipeline: IngestPipeline) -> None:
    for msg in CORPUS:
        if msg["eventType"].startswith("issue_"):
            await pipeline.handle_issue(IssueIngestionMessage.model_validate(msg))
        else:
            await pipeline.handle_comment(CommentIngestionMessage.model_validate(msg))


def _ids(hits: List[SearchHit]) -> List[str]:
    """Collapse multi-chunk sources to their best (first) appearance, preserving rank order."""
    seen, ranked = set(), []
    for h in hits:
        sid = f"{h.chunk_type}:{h.source_id}"
        if sid not in seen:
            seen.add(sid)
            ranked.append(sid)
    return ranked


def _fmt(report: dict) -> str:
    parts = [f"n={report['n_queries']}", f"MRR={report['mrr']:.3f}"]
    for k in KS:
        parts.append(f"Hit@{k}={report[f'hit@{k}']:.3f}")
    for k in KS:
        parts.append(f"Recall@{k}={report[f'recall@{k}']:.3f}")
    return "  ".join(parts)


def _print_report(mode: str, per_query: List[Tuple[List[str], Set[str]]]) -> None:
    semantic = [pq for pq, q in zip(per_query, QUERIES) if q["kind"] == "semantic"]
    lexical = [pq for pq, q in zip(per_query, QUERIES) if q["kind"] == "lexical"]
    print(f"\n=== {mode} ===")
    print("  OVERALL ", _fmt(aggregate(per_query, KS)))
    print("  SEMANTIC", _fmt(aggregate(semantic, KS)))
    print("  LEXICAL ", _fmt(aggregate(lexical, KS)))
    misses = [
        (q["kind"], q["query"], ranked[:3], sorted(rel))
        for (ranked, rel), q in zip(per_query, QUERIES)
        if hit_at_k(ranked, rel, 3) == 0.0
    ]
    if misses:
        print("  misses (no relevant source in top 3):")
        for kind, query, got, want in misses:
            print(f"    [{kind}] {query!r}\n        got={got}\n        want={want}")


async def main() -> None:
    settings = Settings()
    print(f"embedder: provider={settings.embedding_provider} model={settings.embedding_model} "
          f"dim={settings.embedding_dim} base={settings.openai_base_url}")

    pool = await create_pool(settings)
    embedder = build_embedder(settings)
    store = VectorStore(pool)
    pipeline = IngestPipeline(settings, embedder, store)
    # Built directly, independent of VEC_RERANK_ENABLED, so this harness always reports the
    # hybrid+rerank number regardless of what's toggled in the environment it happens to run in.
    reranker = CrossEncoderReranker(settings.rerank_model, settings.rerank_score_threshold)
    rerank_pool = max(TOP_K, settings.rerank_candidate_pool)

    try:
        # Fresh slate so repeated runs are deterministic.
        await pool.execute("TRUNCATE chunks")
        await _ingest(pipeline)
        print(f"ingested corpus -> {await store.count()} chunks")

        vector_runs: List[Tuple[List[str], Set[str]]] = []
        lexical_runs: List[Tuple[List[str], Set[str]]] = []
        hybrid_runs: List[Tuple[List[str], Set[str]]] = []
        rerank_runs: List[Tuple[List[str], Set[str]]] = []

        for q in QUERIES:
            relevant = set(q["relevant"])
            (embedding,) = await embedder.embed([q["query"]])

            vector_hits = await store.search_vector(embedding, SPACE_IDS, TOP_K)
            lexical_hits = await store.search_lexical(q["query"], SPACE_IDS, TOP_K)
            hybrid_hits = await store.search_hybrid(embedding, q["query"], SPACE_IDS, TOP_K)
            # Widen the candidate pool before reranking so the cross-encoder has room to promote a
            # lower-ranked-but-more-relevant chunk — same fetch_limit logic as api/routes.py.
            hybrid_pool_hits = await store.search_hybrid(
                embedding, q["query"], SPACE_IDS, rerank_pool, candidate_pool=rerank_pool
            )
            reranked_hits = await reranker.rerank(q["query"], hybrid_pool_hits, TOP_K)

            vector_runs.append((_ids(vector_hits), relevant))
            lexical_runs.append((_ids(lexical_hits), relevant))
            hybrid_runs.append((_ids(hybrid_hits), relevant))
            rerank_runs.append((_ids(reranked_hits), relevant))

        _print_report("vector-only", vector_runs)
        _print_report("lexical-only (FTS)", lexical_runs)
        _print_report("hybrid (RRF, no rerank)", hybrid_runs)
        _print_report("hybrid + cross-encoder rerank", rerank_runs)
    finally:
        await embedder.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
