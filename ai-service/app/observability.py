"""Request-level observability: Prometheus metrics + a per-request id for log correlation.

Mirrors vectorization-service/app/observability.py (see that module's docstring for the full
rationale — Prometheus over hand-rolled counters, ContextVar over threading an id through every
signature, why this stops short of full OpenTelemetry tracing). The stage split here is different
because this service's pipeline is different: an /ask request spends its time in LLM calls
(possibly several, across corrective-retrieval rounds), retrieval calls to vectorization-service, and
cache lookups — not in embedding/vector-search/rerank, which live on the other side of the /search
call.
"""
from __future__ import annotations

import time
import uuid
from contextvars import ContextVar

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

_request_id: ContextVar[str] = ContextVar("request_id", default="-")

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total", "Total HTTP requests handled", ["method", "path", "status"]
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds", "End-to-end HTTP request latency", ["method", "path"]
)
LLM_CALL_SECONDS = Histogram("agent_llm_call_seconds", "One LLMClient.next_turn() call's latency")
RETRIEVAL_CALL_SECONDS = Histogram("agent_retrieval_call_seconds", "One search_knowledge_base call's latency")
CACHE_LOOKUP_SECONDS = Histogram("agent_cache_lookup_seconds", "Semantic cache get() latency")
# A distribution, not a latency — how many corrective-retrieval rounds a question actually took.
# Directly answers "is corrective retrieval actually firing in practice, and how often" without
# grepping logs; buckets match max_tool_iterations' realistic range (0..~5).
RETRIEVAL_ROUNDS = Histogram(
    "agent_retrieval_rounds", "Corrective-retrieval rounds per /ask call", buckets=(0, 1, 2, 3, 4, 5)
)
CACHE_HITS_TOTAL = Counter("agent_cache_hits_total", "POST /ask responses served from cache", ["hit_type"])
# Same kind of metric as RETRIEVAL_ROUNDS above, for the other bounded loop in this service: how many
# times the multi-agent planner (app/planning/graph.py) actually looped back on a critique. Without
# it there's no way to tell whether the critic is finding real problems or rubber-stamping every
# plan, short of grepping logs. Buckets match MAX_REVISION_ROUNDS' realistic range.
PLANNING_REVISION_ROUNDS = Histogram(
    "planning_revision_rounds", "Critique-driven revision rounds per multi-agent /plan-epic call",
    buckets=(0, 1, 2, 3),
)
# A Counter (monotonically increasing), not a Gauge — cumulative $ spent only ever grows within a
# process's lifetime, same semantics as GET /stats' total_cost_usd (app/stats.py); this is that same
# number, just also exposed as a scrapeable metric so Grafana can graph it over time instead of only
# reading the current snapshot via /stats.
COST_USD_TOTAL = Counter("agent_cost_usd_total", "Cumulative estimated $ spent on LLM calls")
# The "actually notify someone" half of app/sprint_recovery/graph.py's escalation cap — a workflow that
# exhausts max_escalation_rounds without recovering is exactly the event an on-call/sprint-owner should
# see graphed over time (is this happening more often lately?), not just a one-off log line.
SPRINT_RECOVERY_ESCALATIONS_TOTAL = Counter(
    "sprint_recovery_escalations_total",
    "Sprint-recovery workflows that exhausted their escalation cap and handed off to a human",
    ["space_id"],
)


def get_request_id() -> str:
    return _request_id.get()


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = _request_id.set(req_id)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = req_id
            return response
        finally:
            path = request.url.path
            HTTP_REQUEST_DURATION_SECONDS.labels(request.method, path).observe(time.perf_counter() - start)
            HTTP_REQUESTS_TOTAL.labels(request.method, path, str(status_code)).inc()
            _request_id.reset(token)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
