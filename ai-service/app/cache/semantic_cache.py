"""Query-result cache for POST /ask, backed by **Redis Stack** — exact-match first, semantic second
via native vector KNN search.

Why cache at all: repeated or near-duplicate questions ("is the recommendations carousel broken?"
asked by three different people, or the same person retrying similar phrasing) otherwise pay full
retrieval + corrective-retrieval-loop + generation cost every time. A cache hit skips straight to a
previously-computed, already-graded answer — this is a well-known production RAG technique (GPTCache
and similar systems formalize exactly this pattern; this module is a hand-rolled equivalent, kept
that way deliberately — see README "Semantic query cache" for the trade-off reasoning).

**Redis Stack, not plain Redis** (this module's second version — see git history for the first): the
first version stored candidate embeddings in a Redis List and scanned them one by one in Python
(`math.sqrt`/dot-product cosine, by hand) — correct, but O(n) per lookup, a documented scaling limit.
Redis Stack adds the RediSearch module, which does the nearest-neighbor search **inside Redis**
(`FT.SEARCH ... KNN`), the same idea as this project's own pgvector index, just for the cache instead
of the corpus. Verified live against a real `redis-stack-server` container (not assumed from docs):
RediSearch's `COSINE` distance metric returns **distance** (0 = identical, 1 = orthogonal), not
similarity — `similarity = 1 - distance` — and a `TAG` field filter combined with `KNN` in the same
query (`@space_key:{5} => [KNN 1 @embedding $vec AS score]`) genuinely returns **zero** results for a
different `space_key`, confirmed directly, not assumed — the permission boundary is enforced by
Redis's own query engine, not just by application-side filtering after the fact.

Two tiers, deliberately:
  1. **Exact match** (normalized question string + space_ids) — a single Redis `GET`, always checked
     first, no vector search involved.
  2. **Semantic match** (cosine similarity over question embeddings, via vectorization-service's
     POST /embed) — a `FT.SEARCH ... KNN 1` query scoped to this request's `space_key` via a `TAG`
     filter, at the cost of one embedding API call per lookup that isn't an exact hit.

Two risks this design is deliberately strict about, because getting either wrong means silently
serving a WRONG answer, which is worse than a cache miss:

  - **Cross-tenant/permission leakage.** A cached answer computed for one `space_ids` scope must
    never be served to a request with a different scope. Enforced two ways: the exact-match key is
    namespaced by `space_key`, and the semantic-tier `FT.SEARCH` query's `TAG` filter restricts the
    KNN search to the same `space_key` — verified live to return zero cross-space hits, not just
    assumed from the query syntax.
  - **False positives from a high similarity threshold.** The default (`0.97`) is deliberately high —
    a cache miss just costs an extra LLM call; a false-positive cache hit silently returns a wrong
    answer to the user.

Invalidation is still **TTL-only** (Redis `EX`, default 600s), not event-driven — same known
simplification as before, see README "Semantic query cache".

**Testing boundary, stated plainly**: `fakeredis` (used for hermetic tests elsewhere in this codebase)
does not implement RediSearch's `FT.*` commands — confirmed directly (`FT.CREATE` raises "unknown
command"). The exact-match tier and FIFO eviction bookkeeping (`tests/test_semantic_cache.py`) are
still hermetically tested with `fakeredis`, since those paths never call `FT.*`. The semantic-tier KNN
search cannot be hermetic, so CI starts a real `redis-stack-server` and runs the optional integration
test via `TEST_REDIS_STACK_URL`; that test covers both semantic hits and cross-space isolation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import struct
import uuid
from typing import List, Optional, Sequence

from redis.commands.search.field import TagField, VectorField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from app.cache.embedding_client import EmbeddingClient

logger = logging.getLogger(__name__)

SpaceKey = str

# Bumped when the answer-grounding contract changed (structured routing + cited-source filtering).
# Keeping a version in both the index and keys prevents a previously cached, unsupported response
# from bypassing the new validation path after deployment. Old keys remain harmless and expire by
# TTL.
_CACHE_VERSION = "v4"
_INDEX_NAME = f"aicache_vec_idx_{_CACHE_VERSION}"
_VECTOR_KEY_PREFIX = f"aicache:{_CACHE_VERSION}:vec:"


def _normalize(question: str) -> str:
    return " ".join(question.strip().lower().split())


def _space_key(space_ids: Sequence[int]) -> SpaceKey:
    # "_" not "," — RediSearch TAG fields treat "," as the default multi-value separator, which would
    # silently turn "space_key:1,3" into two separate tag values instead of one compound key.
    return "_".join(str(s) for s in sorted(space_ids))


def _pack_vector(vector: List[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


class RedisSemanticCache:
    def __init__(
        self,
        redis_client,
        embed_client: EmbeddingClient,
        ttl_seconds: float,
        similarity_threshold: float,
        max_entries: int,
        embedding_dim: int,
    ):
        self._redis = redis_client
        self._embed = embed_client
        self._ttl = int(ttl_seconds)
        self._threshold = similarity_threshold
        self._max_entries = max_entries
        self._embedding_dim = embedding_dim

    async def ensure_index(self) -> None:
        """Create the vector index once, idempotently. Call during app startup (see main.py) — not
        lazily on first request, so index creation isn't a race between concurrent first callers."""
        try:
            await self._redis.ft(_INDEX_NAME).create_index(
                [
                    TagField("space_key"),
                    VectorField(
                        "embedding", "HNSW",
                        {"TYPE": "FLOAT32", "DIM": self._embedding_dim, "DISTANCE_METRIC": "COSINE"},
                    ),
                ],
                definition=IndexDefinition(prefix=[_VECTOR_KEY_PREFIX], index_type=IndexType.HASH),
            )
            logger.info("created semantic cache vector index", extra={"index": _INDEX_NAME})
        except Exception as exc:  # noqa: BLE001
            if "already exists" not in str(exc).lower():
                raise

    def _exact_key(self, space_key: SpaceKey, norm_q: str) -> str:
        digest = hashlib.sha256(norm_q.encode("utf-8")).hexdigest()[:16]
        return f"aicache:{_CACHE_VERSION}:exact:{space_key}:{digest}"

    def _vector_key(self, space_key: SpaceKey, entry_id: str) -> str:
        return f"{_VECTOR_KEY_PREFIX}{space_key}:{entry_id}"

    def _index_key(self, space_key: SpaceKey) -> str:
        # FIFO eviction queue (Redis List) bounding how many entries per space the cache keeps —
        # unrelated to the FT.SEARCH vector index despite the similar name.
        return f"aicache:{_CACHE_VERSION}:fifo:{space_key}"

    async def get(self, question: str, space_ids: Sequence[int]) -> Optional[dict]:
        space_key = _space_key(space_ids)
        norm_q = _normalize(question)

        exact_raw = await self._redis.get(self._exact_key(space_key, norm_q))
        if exact_raw is not None:
            return json.loads(exact_raw)["response"]

        embedding = await self._embed.embed(question)
        query = (
            Query(f"@space_key:{{{space_key}}} => [KNN 1 @embedding $vec AS score]")
            .sort_by("score")
            .return_fields("response", "score")
            .dialect(2)
        )
        result = await self._redis.ft(_INDEX_NAME).search(query, query_params={"vec": _pack_vector(embedding)})
        if not result.docs:
            return None

        best = result.docs[0]
        similarity = 1.0 - float(best.score)  # RediSearch COSINE returns distance, not similarity
        if similarity >= self._threshold:
            return json.loads(best.response)
        return None

    async def put(self, question: str, space_ids: Sequence[int], response: dict) -> None:
        space_key = _space_key(space_ids)
        norm_q = _normalize(question)
        embedding = await self._embed.embed(question)
        entry_id = uuid.uuid4().hex
        vector_key = self._vector_key(space_key, entry_id)
        index_key = self._index_key(space_key)

        pipe = self._redis.pipeline()
        pipe.set(self._exact_key(space_key, norm_q), json.dumps({"response": response}), ex=self._ttl)
        pipe.hset(
            vector_key,
            mapping={"space_key": space_key, "embedding": _pack_vector(embedding), "response": json.dumps(response)},
        )
        pipe.expire(vector_key, self._ttl)
        pipe.rpush(index_key, entry_id)
        pipe.expire(index_key, self._ttl)
        await pipe.execute()

        await self._evict_overflow(space_key, index_key)

    async def _evict_overflow(self, space_key: SpaceKey, index_key: str) -> None:
        length = await self._redis.llen(index_key)
        overflow = length - self._max_entries
        if overflow <= 0:
            return
        # FIFO: the oldest entries are at the head (put() only ever RPUSHes).
        old_ids = await self._redis.lpop(index_key, overflow)
        if not old_ids:
            return
        for raw_id in old_ids:
            entry_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            # Deleting the HASH key also removes it from the FT index — RediSearch keeps hash-backed
            # indexes in sync with the keyspace automatically, no separate "remove from index" call.
            await self._redis.delete(self._vector_key(space_key, entry_id))


class NoopCache:
    """Used when AI_CACHE_ENABLED=false, and as the hermetic default in tests."""

    async def ensure_index(self) -> None:
        return None

    async def get(self, question: str, space_ids: Sequence[int]) -> Optional[dict]:
        return None

    async def put(self, question: str, space_ids: Sequence[int], response: dict) -> None:
        return None


def build_cache(settings, redis_client, embed_client: EmbeddingClient):
    if not settings.cache_enabled:
        return NoopCache()
    return RedisSemanticCache(
        redis_client=redis_client,
        embed_client=embed_client,
        ttl_seconds=settings.cache_ttl_seconds,
        similarity_threshold=settings.cache_similarity_threshold,
        max_entries=settings.cache_max_entries,
        embedding_dim=settings.cache_embedding_dim,
    )
