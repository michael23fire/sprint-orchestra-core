"""In-process, since-startup counters for cost/usage observability — see GET /stats.

Deliberately in-memory, not a database or metrics backend: this answers "how much has this process
spent since it started," the same question the vectorization-service's own /stats answers for
ingestion counters (app/api/routes.py there). A single asyncio event loop processes requests one
coroutine-step at a time, so plain attribute increments are safe here without a lock — there's no
concurrent *thread* mutating this object, only concurrent *coroutines* that never preempt mid-statement.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.llm.types import Usage
from app.observability import COST_USD_TOTAL


@dataclass
class ServiceStats:
    requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0

    def record(self, usage: Usage, cost_usd: float) -> None:
        self.requests += 1
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        self.total_cost_usd += cost_usd
        # Same number GET /stats reports below, also pushed to Prometheus so Grafana can graph
        # cumulative spend over time rather than only reading the current snapshot via /stats.
        COST_USD_TOTAL.inc(cost_usd)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "avg_cost_usd_per_request": round(self.total_cost_usd / self.requests, 6)
            if self.requests
            else 0.0,
        }
