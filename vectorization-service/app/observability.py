"""Request-level observability: Prometheus metrics + a per-request id for log correlation.

Two things this buys that structured JSON logging alone doesn't:
  1. **Aggregatable metrics** (`GET /metrics`, Prometheus text format) — "what's P95 search latency
     over the last hour" is a query against a time series, not a grep across log files. This is the
     same class of tool (Prometheus + Grafana) most companies actually run in production, so it's
     scraped by the standard `prometheus_client` library rather than hand-rolled.
  2. **A request id that ties one request's log lines together** — set in a ContextVar at the top of
     the middleware, so every `logger.info(..., extra={"request_id": get_request_id()})` call deeper
     in the stack (search stage timings, etc.) can tag itself without threading the id through every
     function signature by hand.

Deliberately NOT full distributed tracing (OpenTelemetry spans exported to Jaeger/Tempo): that's the
natural next step for a multi-service call chain (ai-service -> vectorization-service), noted in the
README, but Prometheus metrics + correlated JSON logs already answer "is this slow, and which stage"
for a single-service, portfolio-scale deployment without pulling in a collector + backend.
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
# Stage-level breakdowns for POST /search specifically — the endpoint with a nontrivial pipeline
# (embed -> vector search -> lexical search -> optional rerank), where "which stage is slow" is the
# actually useful diagnostic question. See app/api/routes.py.
EMBED_SECONDS = Histogram("search_embed_seconds", "Query-embedding latency within /search")
# Labeled by mode rather than split into separate histograms: routes.py calls exactly one of
# search_vector/search_lexical/search_hybrid per request, so "which stage" and "which mode" are the
# same question here — one histogram with a label is simpler than three nearly-identical ones.
RETRIEVAL_SECONDS = Histogram("search_retrieval_seconds", "Store-level search latency", ["mode"])
RERANK_SECONDS = Histogram("search_rerank_seconds", "Cross-encoder reranking latency")


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
