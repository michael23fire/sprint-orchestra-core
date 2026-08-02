"""Reciprocal Rank Fusion — pure function, no I/O, so it's unit-testable without a database.

RRF combines *ranks* rather than raw scores because cosine distance and ts_rank_cd live on
unrelated scales (same reasoning as the tiered FTS ranking in jira-backend's SearchService: never
blend incomparable numbers directly). A chunk that appears in both candidate lists gets the sum of
both reciprocal-rank contributions, so it naturally outranks a chunk only one retriever found —
without any hand-tuned weight between the two signals.
"""
from __future__ import annotations

from typing import Dict, List, Set

from app.models import SearchHit

# Standard choice from the original RRF paper (Cormack et al., 2009) — large enough that a rank-1
# hit doesn't completely dominate a rank-2 hit from the other retriever, small enough that rank
# still matters more than how many retrievers found a result.
RRF_K = 60


def fuse_rrf(*ranked_lists: List[SearchHit], limit: int, k: int = RRF_K) -> List[SearchHit]:
    """Merge any number of already-ranked hit lists into one fused ranking."""
    fused_score: Dict[str, float] = {}
    by_id: Dict[str, SearchHit] = {}
    retrievers_by_id: Dict[str, Set[str]] = {}

    for hits in ranked_lists:
        for rank, hit in enumerate(hits, start=1):
            fused_score[hit.id] = fused_score.get(hit.id, 0.0) + 1.0 / (k + rank)
            by_id[hit.id] = hit
            retrievers_by_id.setdefault(hit.id, set()).update(hit.retrievers)

    ranked_ids = sorted(fused_score, key=lambda cid: fused_score[cid], reverse=True)[:limit]
    return [
        SearchHit(
            id=cid,
            chunk_type=by_id[cid].chunk_type,
            issue_id=by_id[cid].issue_id,
            issue_key=by_id[cid].issue_key,
            space_id=by_id[cid].space_id,
            source_id=by_id[cid].source_id,
            content=by_id[cid].content,
            score=fused_score[cid],
            retrievers=sorted(retrievers_by_id[cid]),
        )
        for cid in ranked_ids
    ]
