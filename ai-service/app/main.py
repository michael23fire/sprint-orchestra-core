"""FastAPI entrypoint for the ai-service (agentic RAG / query-time layer).

Wires: LLM client (Anthropic or local OpenAI-compatible, see app/llm/factory.py) + retrieval client
(calls vectorization-service's /search) into the CragAgent, exposed at POST /ask.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.crag_loop import CragAgent
from app.agent.retrieval_tool import RetrievalClient
from app.api.routes import router
from app.cache.embedding_client import EmbeddingClient
from app.cache.semantic_cache import build_cache
from app.config import get_settings
from app.drafting.instructor_client import build_instructor_client
from app.llm.factory import build_llm_client
from app.logging_config import configure_logging
from app.observability import ObservabilityMiddleware, metrics_response
from app.stats import ServiceStats
from app.tracing import configure_tracing

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    logger.info("starting %s (llm_provider=%s, model=%s)", settings.service_name, settings.llm_provider, settings.agent_model)
    configure_tracing(settings)  # before client construction — see app/tracing.py for coverage scope

    llm = build_llm_client(settings)
    retrieval = RetrievalClient(settings.vectorization_service_url)
    agent = CragAgent(
        llm, retrieval, settings.max_tool_iterations, settings.retrieval_top_k, model=settings.agent_model
    )
    embed_client = EmbeddingClient(settings.vectorization_service_url)
    # decode_responses=False: cache payloads are JSON strings we decode ourselves in
    # semantic_cache.py; list members (entry ids) come back as bytes there too, handled explicitly.
    redis_client = redis.from_url(settings.redis_url) if settings.cache_enabled else None
    cache = build_cache(settings, redis_client, embed_client)
    await cache.ensure_index()  # idempotent; creates the RediSearch vector index once at startup
    instructor_client, instructor_model = build_instructor_client(settings)

    app.state.settings = settings
    app.state.llm = llm
    app.state.retrieval = retrieval
    app.state.agent = agent
    app.state.stats = ServiceStats()
    app.state.cache = cache
    app.state.instructor_client = instructor_client
    app.state.instructor_model = instructor_model

    try:
        yield
    finally:
        await llm.aclose()
        await retrieval.aclose()
        await embed_client.aclose()
        await instructor_client.client.close()
        if redis_client is not None:
            await redis_client.aclose()
        logger.info("stopped %s", settings.service_name)


app = FastAPI(title="AI Service (Agentic RAG)", version="0.1.0", lifespan=lifespan)
app.add_middleware(ObservabilityMiddleware)
# Wide open (`*`) is a demo-only choice, made explicit rather than silently permissive: this lets
# demo/demo.html (opened directly as a file:// page, or served from any dev port) call this API from
# the browser. There's no auth in front of /ask at all yet (see README "known simplifications"), so
# CORS isn't the actual access-control boundary here regardless of how it's configured — a real
# deployment needs real auth first, then a real (non-`*`) CORS origin allowlist matching the actual
# frontend's domain.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.include_router(router)


@app.get("/metrics")
async def metrics():
    return metrics_response()
