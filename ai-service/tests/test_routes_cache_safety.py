"""Regression test for a bug found by the concurrency load test (loadtest/, see loadtest/README.md):
a semantic-cache backend failure (e.g. vectorization-service's /embed erroring or timing out under
load) must degrade POST /ask to "treat as a cache miss / skip the cache write," never crash the
whole request with an unhandled 500 — caching is an optimization, not a correctness dependency.
"""
from app.api.routes import _safe_cache_get, _safe_cache_put


class _BrokenCache:
    async def get(self, question, space_ids):
        raise ConnectionError("embedding service unreachable")

    async def put(self, question, space_ids, response):
        raise ConnectionError("embedding service unreachable")


class _WorkingCache:
    async def get(self, question, space_ids):
        return {"answer": "cached"}

    async def put(self, question, space_ids, response):
        return None


async def test_safe_cache_get_degrades_to_miss_on_backend_failure():
    result = await _safe_cache_get(_BrokenCache(), "question", [1])
    assert result is None


async def test_safe_cache_put_swallows_backend_failure():
    await _safe_cache_put(_BrokenCache(), "question", [1], {"answer": "x"})  # must not raise


async def test_safe_cache_get_passes_through_a_real_hit():
    result = await _safe_cache_get(_WorkingCache(), "question", [1])
    assert result == {"answer": "cached"}
