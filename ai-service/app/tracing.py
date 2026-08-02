"""LLM-call tracing via Arize Phoenix (OpenTelemetry + OpenInference auto-instrumentation).

What this adds on top of the Prometheus/Grafana observability already in `app/observability.py`:
that gives aggregate metrics (latency histograms, request counts) — good for "is this slow, and which
stage," bad for "show me the actual multi-turn conversation and tool calls for *this one* request."
Phoenix's trace UI is built specifically for the latter: open one trace, see the full sequence of LLM
calls, their inputs/outputs, and (for a multi-round CragAgent conversation) how they chain together.
Complementary tools, not a replacement for either.

**Coverage — stated plainly rather than implied:**
`OpenInference`'s instrumentors work by patching the underlying provider SDK client classes
(`anthropic.AsyncAnthropic`, `openai.AsyncOpenAI`). That means:
  - **Covered**: the AI-assisted task-drafting feature (`app/drafting/instructor_client.py`, which
    wraps real `anthropic.AsyncAnthropic` / `openai.AsyncOpenAI` clients via `instructor`) — traces
    show up for this automatically. Verified live.
  - **Covered**: the production CragAgent path (`app/llm/anthropic_client.py`, real `anthropic` SDK)
    — instrumented the same way, but (like the rest of that path) not run live in this project (no
    `ANTHROPIC_API_KEY` configured here).
  - **Covered**: the local-testing CragAgent path (`app/llm/openai_compatible_client.py`) — this used
    to talk to LM Studio over raw `httpx`, which OpenInference's SDK-level patching has nothing to
    attach to (a hand-rolled HTTP call is OpenAI-*shaped* on the wire but touches none of the `openai`
    package's classes). It now goes through the real `openai.AsyncOpenAI` client instead, pointed at
    the same LM Studio `base_url` — same requests, same responses, now traced. This closed what used
    to be the actual live-tested majority of this project's agent traffic (every `/ask` call against
    LM Studio) being invisible to Phoenix.
"""
from __future__ import annotations

import logging

from app.config import Settings

logger = logging.getLogger(__name__)


def configure_tracing(settings: Settings) -> None:
    if not settings.phoenix_enabled:
        return

    from openinference.instrumentation.anthropic import AnthropicInstrumentor
    from openinference.instrumentation.openai import OpenAIInstrumentor
    from phoenix.otel import register

    register(
        endpoint=settings.phoenix_collector_endpoint,
        project_name=settings.phoenix_project_name,
        protocol="grpc",
    )
    # Idempotent: instruments the SDK classes globally, regardless of which provider this process
    # actually uses at runtime — harmless to enable both.
    AnthropicInstrumentor().instrument()
    OpenAIInstrumentor().instrument()
    logger.info(
        "Phoenix tracing enabled",
        extra={"endpoint": settings.phoenix_collector_endpoint, "project": settings.phoenix_project_name},
    )
