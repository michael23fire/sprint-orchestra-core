"""FastAPI entrypoint for the ai-service (agentic RAG / query-time layer).

Wires: LLM client (Anthropic or local OpenAI-compatible, see app/llm/factory.py) + retrieval client
(calls vectorization-service's /search) into the CragAgent, exposed at POST /ask.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.crag_loop import CragAgent
from app.agent.retrieval_tool import RetrievalClient
from app.api.routes import router
from app.api.sprint_recovery_routes import router as sprint_recovery_router
from app.auth.space_membership import build_space_membership_checker
from app.cache.embedding_client import EmbeddingClient
from app.cache.semantic_cache import build_cache
from app.config import get_settings
from app.drafting.instructor_client import build_instructor_client
from app.llm.factory import build_llm_client
from app.logging_config import configure_logging
from app.observability import ObservabilityMiddleware, metrics_response
from app.planning.checkpoint import build_checkpointer
from app.planning.graph import build_planning_graph
from app.planning.jira_commit_client import JiraCommitClient
from app.planning.rollout_graph import build_rollout_graph
from app.sprint_recovery.active_threads import (
    ensure_active_threads_table,
    ensure_waiting_threads_table,
)
from app.sprint_recovery.graph import build_sprint_recovery_graph
from app.sprint_recovery.jira_actions_client import JiraActionsClient
from app.sprint_recovery.kafka_trigger import SprintRecoveryKafkaTrigger
from app.stats import ServiceStats
from app.tracing import configure_tracing

logger = logging.getLogger(__name__)


def _workflow_checkpoint_db_url(settings) -> str | None:
    """Select the checkpoint DSN for the durable workflows that are actually enabled.

    The two feature flags have separate settings so either graph can run independently. When both
    share one checkpointer they must also share one DSN; silently picking the epic setting while
    ignoring a different sprint-recovery setting would connect to the wrong database.
    """
    if settings.epic_rollout_enabled and settings.sprint_recovery_enabled:
        if settings.epic_rollout_checkpoint_db_url != settings.sprint_recovery_checkpoint_db_url:
            raise ValueError(
                "AI_EPIC_ROLLOUT_CHECKPOINT_DB_URL and "
                "AI_SPRINT_RECOVERY_CHECKPOINT_DB_URL must match when both workflows are enabled"
            )
        return settings.epic_rollout_checkpoint_db_url
    if settings.epic_rollout_enabled:
        return settings.epic_rollout_checkpoint_db_url
    if settings.sprint_recovery_enabled:
        return settings.sprint_recovery_checkpoint_db_url
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    logger.info("starting %s (llm_provider=%s, model=%s)", settings.service_name, settings.llm_provider, settings.agent_model)
    configure_tracing(settings)  # before client construction — see app/tracing.py for coverage scope

    llm = build_llm_client(settings)
    retrieval = RetrievalClient(settings.vectorization_service_url)
    agent = CragAgent(
        llm, retrieval, settings.max_tool_iterations, settings.retrieval_top_k, model=settings.agent_model,
        faithfulness_check_enabled=settings.faithfulness_check_enabled,
    )
    embed_client = EmbeddingClient(settings.vectorization_service_url)
    # decode_responses=False: cache payloads are JSON strings we decode ourselves in
    # semantic_cache.py; list members (entry ids) come back as bytes there too, handled explicitly.
    redis_client = redis.from_url(settings.redis_url) if settings.cache_enabled else None
    cache = build_cache(settings, redis_client, embed_client)
    await cache.ensure_index()  # idempotent; creates the RediSearch vector index once at startup
    instructor_client, instructor_model = build_instructor_client(settings)
    space_membership = build_space_membership_checker(settings)
    # Compiled once, shared across concurrent requests — safe because per-request data flows through
    # ainvoke's state argument, not instance attributes (see app/planning/graph.py's docstring).
    planning_graph = (
        build_planning_graph(instructor_client, instructor_model)
        if settings.epic_planning_multiagent_enabled
        else None
    )

    exit_stack = AsyncExitStack()
    rollout_graph = None
    jira_commit_client = None
    # Shared by both LangGraph durable-workflow features below — one checkpoint store/schema for this
    # codebase's workflow-lifecycle bookkeeping, not two, since neither graph's threads collide (they
    # key on thread_id, and each feature mints its own).
    checkpointer = None
    checkpoint_db_url = _workflow_checkpoint_db_url(settings)
    if checkpoint_db_url is not None:
        checkpointer = await build_checkpointer(checkpoint_db_url, exit_stack)
    if settings.epic_rollout_enabled:
        jira_commit_client = JiraCommitClient(settings.jira_backend_url, settings.internal_gateway_token)
        rollout_graph = build_rollout_graph(
            instructor_client,
            instructor_model,
            space_membership,
            jira_commit_client,
            # Read at call time, not captured now — see build_rollout_graph's docstring.
            lambda: app.state.planning_graph,
        ).compile(checkpointer=checkpointer)

    sprint_recovery_graph = None
    jira_actions_client = None
    sprint_recovery_kafka_trigger = None
    # Created before the graph, not alongside the other app.state assignments below, so this
    # workflow's real token/cost usage can be recorded into the same counters GET /stats and
    # `agent_cost_usd_total` already report for every other LLM call — see `_call_model`.
    stats = ServiceStats()
    if settings.sprint_recovery_enabled:
        jira_actions_client = JiraActionsClient(settings.jira_backend_url, settings.internal_gateway_token)
        sprint_recovery_graph = build_sprint_recovery_graph(
            instructor_client, instructor_model, space_membership, retrieval, jira_actions_client,
            on_usage=stats.record,
        ).compile(checkpointer=checkpointer)
        # See active_threads.py's own docstring: the checkpointer itself has no way to look up "the
        # thread for sprint X" — this is the one small side table that makes crash-resume something a
        # human can actually find their way back to through the UI, not just something the graph can
        # technically resume if you already have the thread_id.
        if checkpoint_db_url is not None:
            await ensure_active_threads_table(checkpoint_db_url)
            await ensure_waiting_threads_table(checkpoint_db_url)
        if settings.sprint_recovery_kafka_enabled:
            sprint_recovery_kafka_trigger = SprintRecoveryKafkaTrigger(
                settings.kafka_bootstrap_servers, settings.kafka_content_topic,
                settings.sprint_recovery_kafka_group_id, sprint_recovery_graph,
                escalation_webhook_url=settings.sprint_recovery_escalation_webhook_url,
                # Without this the consumer would only ever know about pauses created by *this*
                # process — see kafka_trigger.py's docstring on the restart-deafness bug.
                db_url=checkpoint_db_url,
            )
            await sprint_recovery_kafka_trigger.start()

    app.state.settings = settings
    app.state.llm = llm
    app.state.retrieval = retrieval
    app.state.agent = agent
    app.state.stats = stats
    app.state.cache = cache
    app.state.instructor_client = instructor_client
    app.state.instructor_model = instructor_model
    app.state.space_membership = space_membership
    app.state.planning_graph = planning_graph
    app.state.rollout_graph = rollout_graph
    app.state.sprint_recovery_graph = sprint_recovery_graph
    app.state.sprint_recovery_kafka_trigger = sprint_recovery_kafka_trigger
    app.state.sprint_recovery_checkpoint_db_url = checkpoint_db_url

    try:
        yield
    finally:
        await llm.aclose()
        await retrieval.aclose()
        await embed_client.aclose()
        await instructor_client.client.close()
        await space_membership.aclose()
        if jira_commit_client is not None:
            await jira_commit_client.aclose()
        if jira_actions_client is not None:
            await jira_actions_client.aclose()
        if sprint_recovery_kafka_trigger is not None:
            await sprint_recovery_kafka_trigger.stop()
        await exit_stack.aclose()
        if redis_client is not None:
            await redis_client.aclose()
        logger.info("stopped %s", settings.service_name)


app = FastAPI(title="AI Service (Agentic RAG)", version="0.1.0", lifespan=lifespan)
app.add_middleware(ObservabilityMiddleware)
# Wide open (`*`) is a demo-only choice, made explicit rather than silently permissive: this lets
# demo/demo.html (opened directly as a file:// page, or served from any dev port) call this API from
# the browser. Space-level authorization is now enforced (see app/auth/space_membership.py), but only
# when the gateway's X-User-Id header is present — CORS still isn't the actual access-control boundary
# for a direct, no-header caller, so a real deployment still needs a real (non-`*`) CORS origin
# allowlist matching the actual
# frontend's domain.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.include_router(router)
app.include_router(sprint_recovery_router)


@app.get("/metrics")
async def metrics():
    return metrics_response()
