"""Semantic cache tests — fakeredis plus an optional real Redis Stack integration test.

fakeredis does not implement RediSearch's `FT.*` commands (confirmed directly — `FT.CREATE` raises
"unknown command"), so the semantic-tier KNN search itself (`RedisSemanticCache.get()`'s vector
search path) cannot have hermetic coverage here. CI sets ``TEST_REDIS_STACK_URL`` and executes the
real integration test below against `redis-stack-server` (including COSINE
distance-vs-similarity semantics, and that a `space_key` TAG filter genuinely returns zero cross-space
hits). What is hermetically tested with fakeredis: the exact-match tier (pure `GET`/`SET`, no `FT.*` calls)
and the FIFO eviction bookkeeping (`RPUSH`/`LPOP`/`DEL`, also no `FT.*` calls) — both real code paths
`put()` and `get()` exercise on every call regardless of whether the semantic tier ever fires.
"""
import asyncio
import os
import uuid

import fakeredis
import pytest
import redis.asyncio as redis

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

    # Inspect the exact tier directly instead of falling through to FT.SEARCH, which fakeredis does
    # not implement. The real-Redis integration test below owns semantic-tier isolation coverage.
    normalized = "is checkout broken"
    assert await redis_client.get(cache._exact_key("1", normalized)) is not None
    assert await redis_client.get(cache._exact_key("2", normalized)) is None


async def test_real_redis_stack_semantic_hit_and_cross_space_isolation():
    """Exercises the native FT.SEARCH path CI's Redis Stack service is specifically for."""
    redis_url = os.getenv("TEST_REDIS_STACK_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_STACK_URL is not configured")

    client = redis.from_url(redis_url)
    question = f"checkout failure {uuid.uuid4().hex}"
    paraphrase = f"why checkout failed {uuid.uuid4().hex}"
    vector = [1.0, *([0.0] * 1023)]
    cache = _cache(
        client,
        {question: vector, paraphrase: vector},
        ttl=60,
        threshold=0.97,
        max_entries=10,
        embedding_dim=1024,
    )
    space_ids = [987654321]
    other_space_ids = [987654322]
    await cache.ensure_index()
    try:
        await cache.put(question, space_ids, {"answer": "connection pool exhaustion"})
        # RediSearch indexing can be asynchronous; poll briefly instead of making the test timing-
        # sensitive on a newly started CI container.
        hit = None
        for _ in range(20):
            hit = await cache.get(paraphrase, space_ids)
            if hit is not None:
                break
            await asyncio.sleep(0.05)
        assert hit == {"answer": "connection pool exhaustion"}
        assert await cache.get(paraphrase, other_space_ids) is None
    finally:
        space_key = "987654321"
        entry_ids = await client.lrange(cache._index_key(space_key), 0, -1)
        keys = [
            cache._exact_key(space_key, " ".join(question.lower().split())),
            cache._index_key(space_key),
            *(cache._vector_key(space_key, raw.decode() if isinstance(raw, bytes) else raw) for raw in entry_ids),
        ]
        if keys:
            await client.delete(*keys)
        await client.aclose()


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
