"""Concurrency/load test for vectorization-service's POST /search.

Why this endpoint specifically: it's the one on the hot path for every /ask call (ai-service calls
it on every corrective-retrieval round), it's cheap enough to run at real concurrency in a few
seconds, and it exercises the part of the stack most likely to hide a concurrency bug — the asyncpg
connection pool (VEC_PG_POOL_MAX_SIZE, default 8) and the optional cross-encoder reranker (a
CPU-bound call pushed onto a worker thread via asyncio.to_thread — see app/db/reranker.py). Those are
exactly the two places a naive implementation could silently serialize requests that look concurrent
from the outside.

Run (see loadtest/README.md for the full walkthrough and results captured against this repo):
    pip install -r loadtest/requirements.txt
    locust -f loadtest/locustfile_vectorization.py --host http://localhost:8100 \
        --headless -u 50 -r 10 -t 30s --csv loadtest/results/vectorization
"""
from __future__ import annotations

import random

from locust import HttpUser, between, task

# Real queries from vectorization-service/eval/dataset.py — the same labeled AtlasCart benchmark
# used to measure retrieval quality, reused here so load-test traffic looks like real usage instead
# of a single repeated query (which would trivially cache-hit at every layer and understate cost).
QUERIES = [
    "why did the payment checkout fall over during the holiday rush",
    "how did we stop customers from being billed more than once",
    "make the site easier on the eyes at night",
    "users getting logged out randomly",
    "inventory going negative during flash sales",
    "recommendation carousel showing out of stock items",
    "ATLAS-6",
    "HikariCP",
]
MODES = ["hybrid", "vector", "lexical"]


class SearchUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task
    def search(self):
        self.client.post(
            "/search",
            json={
                "query": random.choice(QUERIES),
                "space_ids": [1],
                "limit": 5,
                "mode": random.choice(MODES),
            },
            name="/search",
        )
