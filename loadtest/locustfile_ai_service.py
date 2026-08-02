"""Concurrency test for ai-service's POST /ask — the full agentic CRAG loop end to end.

Deliberately run at LOW concurrency (a handful of users, not fifty) and read differently from the
vectorization-service load test: an LLM call is seconds, not milliseconds, so raw throughput here is
bounded by the upstream LLM server's own concurrency (in local dev, a single LM Studio process
serving one loaded model), not by this codebase. What this test actually validates — the properties
that matter for a "production grade" claim — is *correctness under concurrency*: do concurrent
requests' `messages` histories stay isolated (no cross-request state bleed in CragAgent.ask, which
allocates fresh local lists per call — see app/agent/crag_loop.py), does the semantic cache serve the
right answer to the right space_ids under concurrent writers (app/cache/semantic_cache.py), and does
nothing deadlock or 500. See loadtest/README.md for what was actually observed running this.

Run:
    pip install -r loadtest/requirements.txt
    locust -f loadtest/locustfile_ai_service.py --host http://localhost:8200 \
        --headless -u 5 -r 1 -t 60s --csv loadtest/results/ai_service
"""
from __future__ import annotations

import random

from locust import HttpUser, between, task

QUESTIONS = [
    "Why did checkout fail during the holiday sale?",
    "How did we stop customers from being billed more than once?",
    "Are users still getting logged out randomly?",
    "Does the product support a GDPR data export?",  # expected: honest abstention, not in corpus
    "What languages does the mobile app support?",  # expected: honest abstention, not in corpus
]


class AskUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def ask(self):
        self.client.post(
            "/ask",
            json={"question": random.choice(QUESTIONS), "space_ids": [1]},
            name="/ask",
            timeout=60,
        )
