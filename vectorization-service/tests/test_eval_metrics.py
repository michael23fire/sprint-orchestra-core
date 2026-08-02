"""Hermetic tests for the retrieval metrics — no embedder, DB, or network."""
from eval.metrics import aggregate, hit_at_k, recall_at_k, reciprocal_rank


def test_hit_at_k():
    assert hit_at_k(["a", "b", "c"], {"c"}, 3) == 1.0
    assert hit_at_k(["a", "b", "c"], {"c"}, 2) == 0.0  # c is at rank 3, outside top 2
    assert hit_at_k(["a", "b"], {"z"}, 5) == 0.0


def test_recall_at_k():
    # Two relevant, one found in top 3 -> 0.5
    assert recall_at_k(["a", "b", "c"], {"a", "z"}, 3) == 0.5
    assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0
    assert recall_at_k(["a"], set(), 3) == 0.0


def test_reciprocal_rank():
    assert reciprocal_rank(["a", "b", "c"], {"a"}) == 1.0       # rank 1
    assert reciprocal_rank(["a", "b", "c"], {"b"}) == 0.5       # rank 2
    assert reciprocal_rank(["a", "b", "c"], {"z"}) == 0.0       # not found


def test_aggregate_averages_across_queries():
    per_query = [
        (["a", "b"], {"a"}),   # rank 1
        (["x", "y"], {"y"}),   # rank 2
    ]
    report = aggregate(per_query, ks=(1, 3))
    assert report["n_queries"] == 2
    assert report["mrr"] == (1.0 + 0.5) / 2
    assert report["hit@1"] == 0.5   # only the first query hits at rank 1
    assert report["hit@3"] == 1.0   # both hit within top 3
