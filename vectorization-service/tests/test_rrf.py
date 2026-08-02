"""Reciprocal Rank Fusion — pure logic, no DB. See app/db/rrf.py."""
from app.db.rrf import fuse_rrf
from app.models import SearchHit


def _hit(cid: str, retriever: str) -> SearchHit:
    return SearchHit(
        id=cid, chunk_type="issue", issue_id=1, issue_key="X-1", space_id=1,
        source_id=1, content=f"content for {cid}", score=0.0, retrievers=[retriever],
    )


def test_hit_found_by_both_retrievers_outranks_single_retriever_hit():
    # "a" is rank 1 in both lists; "b" is rank 2 in vector only; "c" is rank 1 lexical only.
    vector = [_hit("a", "vector"), _hit("b", "vector")]
    lexical = [_hit("c", "lexical"), _hit("a", "lexical")]

    fused = fuse_rrf(vector, lexical, limit=10)

    assert fused[0].id == "a"  # found by both -> highest combined score
    assert sorted(fused[0].retrievers) == ["lexical", "vector"]


def test_agreement_between_retrievers_outweighs_rank_at_k60():
    # With k=60, 1/(60+r) is nearly flat for small r — so a hit both retrievers agree on (even at a
    # middling rank) outscores a hit only one retriever ranked #1. This is RRF's actual, slightly
    # counterintuitive behavior at the standard k: agreement matters more than rank position within
    # the top of the list. Confirmed by hand: 1/61 ≈ 0.0164 (rank-1, one list) vs
    # 1/65 + 1/65 ≈ 0.0308 (rank-5, both lists) — the dual hit wins.
    vector = [_hit("v1", "vector"), _hit("v2", "vector"), _hit("v3", "vector"),
              _hit("v4", "vector"), _hit("dual", "vector")]
    lexical = [_hit("l1", "lexical"), _hit("l2", "lexical"), _hit("l3", "lexical"),
               _hit("l4", "lexical"), _hit("dual", "lexical")]

    fused = fuse_rrf(vector, lexical, limit=10)
    assert fused[0].id == "dual"
    assert sorted(fused[0].retrievers) == ["lexical", "vector"]


def test_limit_truncates_result():
    vector = [_hit(f"v{i}", "vector") for i in range(5)]
    fused = fuse_rrf(vector, limit=2)
    assert len(fused) == 2


def test_single_retriever_only_still_works():
    # fuse_rrf must accept just one ranked list (e.g. pure vector or pure lexical mode upstream).
    vector = [_hit("a", "vector"), _hit("b", "vector")]
    fused = fuse_rrf(vector, limit=10)
    assert [h.id for h in fused] == ["a", "b"]
