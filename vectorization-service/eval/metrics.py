"""Retrieval-quality metrics.

Pure functions over (ranked retrieved ids, set of relevant ids) so they're trivially unit-testable
without any embedder, DB, or network — see tests/test_eval_metrics.py. These are the standard
information-retrieval metrics any RAG evaluation should report:

  * Hit@k  — did *any* relevant item appear in the top k? (binary per query)
  * Recall@k — fraction of the query's relevant items found in the top k
  * Reciprocal Rank — 1 / rank of the first relevant item (0 if none), averaged into MRR
"""
from __future__ import annotations

from typing import Iterable, List, Sequence, Set


def hit_at_k(retrieved: Sequence[str], relevant: Set[str], k: int) -> float:
    return 1.0 if set(retrieved[:k]) & relevant else 0.0


def recall_at_k(retrieved: Sequence[str], relevant: Set[str], k: int) -> float:
    if not relevant:
        return 0.0
    found = len(set(retrieved[:k]) & relevant)
    return found / len(relevant)


def reciprocal_rank(retrieved: Sequence[str], relevant: Set[str]) -> float:
    for i, rid in enumerate(retrieved, start=1):
        if rid in relevant:
            return 1.0 / i
    return 0.0


def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def aggregate(
    per_query: List[tuple[Sequence[str], Set[str]]], ks: Sequence[int] = (1, 3, 5)
) -> dict:
    """Aggregate metrics across many queries. ``per_query`` is [(ranked_ids, relevant_ids), ...]."""
    report = {"n_queries": len(per_query), "mrr": mean(reciprocal_rank(r, rel) for r, rel in per_query)}
    for k in ks:
        report[f"hit@{k}"] = mean(hit_at_k(r, rel, k) for r, rel in per_query)
        report[f"recall@{k}"] = mean(recall_at_k(r, rel, k) for r, rel in per_query)
    return report
