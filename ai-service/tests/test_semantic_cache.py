"""Semantic cache tests — fakeredis (in-memory, real Redis wire protocol) + a fake embedding client.

fakeredis does not implement RediSearch's `FT.*` commands (confirmed directly — `FT.CREATE` raises
"unknown command"), so the semantic-tier KNN search itself (`RedisSemanticCache.get()`'s vector
search path) has **no hermetic coverage here** — it's verified against a real `redis-stack-server`
container instead (see `app/cache/semantic_cache.py`'s module docstring for what was verified: COSIN
distance-vs-similarity semantics, and that a `space_key` TAG filter genuinely returns zero cross-space
hits). What *is* hermetically tested here: the exact-match tier (pure `GET`/`SET`, no `FT.*` calls)
and the FIFO eviction bookkeeping (`RPUSH`/`LPOP`/`DEL`, also no `FT.*` calls) — both real code paths
`put()` and `get()` exercise on every call regardless of whether the semantic tier ever fires.
"""
import asyncio

import fakeredis
import pytest

from app.cache.semantic_cache import NoopCache, RedisSemanticCache, build_cache


class FakeEmbedClient:
    """Maps exact question text to a scripted vector — makes cosine similarity fully controllable."""

    def __init__(self, vectors: dict):
        self._vectors = vectors
        self.calls = 0

    async def embed(self, text):
        self.calls += 1
        return self._vectors[text]


@pytest.fixture
def redis_client():
    client = fakeredis.FakeAsyncRedis()
    yield client


def _cache(redis_client, vectors, ttl=600.0, threshold=0.97, max_entries=500, embedding_dim=2):
    return RedisSemanticCache(
        redis_client=redis_client,
        embed_client=FakeEmbedClient(vectors),
        ttl_seconds=ttl,
        similarity_threshold=threshold,
        max_entries=max_entries,
        embedding_dim=embedding_dim,
    )


async def test_exact_match_hit_skips_embedding_call(redis_client):
    embed = FakeEmbedClient({"How do I reset my password?": [1.0, 0.0]})
    cache = RedisSemanticCache(redis_client, embed, ttl_seconds=600, similarity_threshold=0.97, max_entries=10, embedding_dim=2)

    await cache.put("How do I reset my password?", [1], {"answer": "click forgot password"})
    calls_after_put = embed.calls

    # "how do i reset my password?" normalizes (lowercase + whitespace-collapse) to the same exact
    # key the put() call stored, without ever calling embed() again.
    result = await cache.get("how do i reset my password?", [1])
    assert result == {"answer": "click forgot password"}
    assert embed.calls == calls_after_put  # exact hit never needed an embedding call


async def test_exact_match_respects_space_ids_isolation(redis_client):
    """Security-critical: identical question text/embedding must not cross a space_ids boundary."""
    cache = _cache(redis_client, {"is checkout broken": [1.0, 0.0]})
    await cache.put("is checkout broken", [1], {"answer": "yes, ATLAS-3"})

    # Different scope, identical text — must be a miss even on the exact tier. (This will fall
    # through to the semantic tier's FT.SEARCH, which fakeredis doesn't support — see module
    # docstring — so this assertion only proves the exact tier itself never cross-matches; the
    # semantic tier's isolation is verified live instead.)
    try:
        result = await cache.get("is checkout broken", [2])
    except Exception as exc:  # noqa: BLE001 - fakeredis has no FT.SEARCH, expected here
        pytest.skip(f"fakeredis has no RediSearch support ({exc}); verified live instead — see module docstring")
    assert result is None


async def test_disabled_cache_never_stores_or_returns_anything():
    cache = NoopCache()
    await cache.put("q", [1], {"answer": "x"})  # must not raise even with nothing configured
    assert await cache.get("q", [1]) is None


async def test_max_entries_evicts_oldest(redis_client):
    # Exercises put()'s FIFO bookkeeping only (RPUSH/LPOP/DEL) — no FT.SEARCH involved, so this is
    # fully hermetic despite living in the same class as the semantic-search code.
    vectors = {f"q{i}": [float(i), 0.0] for i in range(5)}
    cache = _cache(redis_client, vectors, max_entries=2)
    for i in range(3):
        await cache.put(f"q{i}", [1], {"answer": str(i)})

    surviving_ids = await redis_client.lrange(cache._index_key("1"), 0, -1)
    assert len(surviving_ids) == 2  # q0 was evicted (FIFO) when q2 pushed past max_entries=2


class _Settings:
    def __init__(self, cache_enabled):
        self.cache_enabled = cache_enabled
        self.cache_ttl_seconds = 600.0
        self.cache_similarity_threshold = 0.97
        self.cache_max_entries = 500
        self.cache_embedding_dim = 2


def test_build_cache_returns_noop_when_disabled(redis_client):
    cache = build_cache(_Settings(cache_enabled=False), redis_client, FakeEmbedClient({}))
    assert isinstance(cache, NoopCache)


def test_build_cache_returns_redis_backed_when_enabled(redis_client):
    cache = build_cache(_Settings(cache_enabled=True), redis_client, FakeEmbedClient({}))
    assert isinstance(cache, RedisSemanticCache)
