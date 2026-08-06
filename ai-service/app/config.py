"""Runtime configuration for the ai-service (query-time agent layer)."""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AI_", env_file=".env", extra="ignore")

    service_name: str = "ai-service"
    log_level: str = "INFO"
    log_json: bool = True

    # --- Downstream: the vectorization-service's raw retrieval API (see vectorization-service/app/api/routes.py /search) ---
    vectorization_service_url: str = "http://localhost:8100"

    # --- LLM provider ---
    # anthropic: the real, production path (official SDK, claude-opus-4-8 default).
    # openai_compatible: a local OpenAI-compatible server (LM Studio/Ollama/vLLM) — for running and
    # testing the agent loop with no cloud API key. Same agent/CRAG control-flow code runs on either.
    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    agent_model: str = "claude-opus-4-8"
    openai_base_url: str = "http://localhost:1234/v1"
    openai_api_key: str = "local"

    # --- Agentic / Corrective RAG loop ---
    max_tool_iterations: int = 4  # hard cap on retrieve-and-reassess rounds — bounds cost/latency
    retrieval_top_k: int = 5
    max_output_tokens: int = 2048
    # Local OpenAI-compatible servers (LM Studio/Ollama/vLLM) generating long completions on CPU/MLX
    # can take well over a minute; a real ReadTimeout here isn't retried (see
    # openai_compatible_client.py's _is_transient_server_error) since retrying a slow model just
    # doubles the wait, so this needs to be generous enough to cover a full max_output_tokens
    # generation rather than caught by retries.
    llm_request_timeout_seconds: float = 300.0

    # --- Runtime faithfulness guardrail (app/agent/crag_loop.py's `_check_faithfulness`) ---
    # RAGAS's faithfulness metric (see eval/ragas_eval.py) is an OFFLINE measurement — it tells you,
    # after the fact and against a curated golden set, how faithful past answers were. It cannot catch
    # a hallucination in a live answer before the caller sees it. This registers one more post-
    # generation verifier (alongside `_check_subtask_claims`/`_check_current_state_claims`) that asks
    # a fast check, at answer time, whether THIS SPECIFIC answer's claims are actually supported by
    # what was retrieved — same "verify, then correct once if needed" mechanism those two already use,
    # just checking prose faithfulness instead of a specific structured relationship.
    #
    # Off by default: this is one more real LLM call on every non-abstaining answer, the same cost
    # argument contextual_retrieval_enabled already makes for ingestion — a real latency/cost tradeoff,
    # not something to enable silently.
    faithfulness_check_enabled: bool = False

    # --- Multi-agent epic planning (app/planning/graph.py) ---
    # Routes POST /plan-epic through a LangGraph planner -> critic pipeline with a bounded revision
    # loop, instead of app/planning/service.py's single structured-output call. The response shape is
    # identical either way; this only changes how the plan is produced.
    #
    # Off by default, and that default is a measured result rather than caution: on 8 seeded proposals
    # the multi-agent path cost 2-4 LLM calls instead of 1 for a +0.13/+0.26 (of 5) quality delta,
    # short of the improvement threshold pre-registered in eval/planning_multiagent_eval.py before the
    # run. Kept, flagged off, because the eval is small and judge-saturated enough that "no measured
    # win" is not the same claim as "no win" — see eval/results/planning_multiagent_comparison.md.
    epic_planning_multiagent_enabled: bool = False

    # --- Semantic query cache (app/cache/semantic_cache.py), backed by Redis ---
    # On by default — unlike contextual retrieval's per-chunk LLM cost, a cache hit *saves* an LLM
    # call rather than spending one, so there's no cost argument for defaulting it off.
    cache_enabled: bool = True
    redis_url: str = "redis://localhost:6379/0"  # same `redis` container docker-compose.yml already runs
    cache_ttl_seconds: float = 600.0  # TTL-only invalidation; see module docstring
    cache_similarity_threshold: float = 0.97  # deliberately high — see module docstring
    cache_max_entries: int = 500
    # Must match whatever vectorization-service's configured embedder actually returns (its own
    # VEC_EMBEDDING_DIM) — the Redis Stack vector index is created with a fixed dimension up front and
    # will reject embeddings of a different size. Default matches this project's default local/Voyage
    # embedding dimension (1024).
    cache_embedding_dim: int = 1024

    # --- Space-membership validation (auth boundary) ---
    # `space_ids` on /ask, /ask/stream, /search was previously trusted unconditionally from the
    # caller (see gateway/application.yml's own documented trust-boundary comment and this project's
    # README "known simplifications") — any caller could put any space_id in the request body and
    # this service would happily query it. Gateway already validates the JWT and forwards the real
    # authenticated user's id via X-User-Id (see PropagateUserHeadersGatewayFilter.java); jira-backend
    # already has a membership-scoped endpoint (GET /api/spaces?userId=X, backed by
    # SpaceMemberRepository.findActiveSpaceIdsByUserId) that the frontend's own SpaceContext already
    # uses. This wires the same two existing pieces together to close the gap, rather than building
    # new membership infrastructure.
    #
    # If X-User-Id is absent, the check is skipped: ai-service isn't meant to be reachable directly
    # except by trusted internal callers in that case (this project's own eval/smoke-test scripts all
    # call ai-service directly on :8200, bypassing the gateway entirely, exactly like a same-network
    # internal caller would) — production traffic always goes through the gateway and always carries
    # X-User-Id, so real end-user requests are covered either way.
    space_membership_check_enabled: bool = True
    jira_backend_url: str = "http://localhost:8081"
    # Must match jira-backend's app.security.internal-gateway-token / gateway's INTERNAL_GATEWAY_TOKEN
    # — same shared secret gateway already presents to jira-backend as X-Gateway-Internal.
    internal_gateway_token: str = "dev-internal-gateway-token-min-32-chars-long!!"
    space_membership_cache_ttl_seconds: float = 60.0

    # --- Epic rollout: durable, human-approved commit-to-Jira workflow (app/planning/rollout_graph.py) ---
    # A different concern from epic_planning_multiagent_enabled above: that flag controls how a plan is
    # GENERATED (single call vs planner<->critic). This controls what happens AFTER a human approves
    # one — pausing durably (surviving a process restart) until a decision arrives, then writing the
    # approved issues to Jira exactly once. Orthogonal to the multiagent flag; wraps either plan-
    # generation path identically.
    epic_rollout_enabled: bool = True
    # Reuses jira-backend's existing `postgres` container/DB (see docker-compose.yml) in a dedicated
    # schema, rather than standing up a second Postgres — this is workflow-lifecycle bookkeeping, not
    # product data, but a whole extra container for it isn't worth operating for what it is.
    epic_rollout_checkpoint_db_url: str = "postgresql://poc:poc123@localhost:5432/pocdb"

    # --- Sprint recovery: diagnosis -> grounded hypotheses -> recovery plans -> durable approval ->
    # idempotent execution -> event-driven re-evaluation (app/sprint_recovery/graph.py) ---
    sprint_recovery_enabled: bool = True
    # Same checkpoint Postgres/schema as epic rollout — both are workflow-lifecycle bookkeeping for
    # this codebase's LangGraph features, no reason to operate two separate checkpoint stores.
    sprint_recovery_checkpoint_db_url: str = "postgresql://poc:poc123@localhost:5432/pocdb"
    sprint_recovery_max_clarification_rounds: int = 2
    sprint_recovery_max_escalation_rounds: int = 2
    # Caps the human-reject-with-feedback -> replan loop (approval_node's "revise" path) — same reason
    # every other loop in this graph is bounded, just triggered by a human's dissatisfaction instead of
    # an automated reevaluate finding the sprint still at risk.
    sprint_recovery_max_plan_revision_rounds: int = 2
    sprint_recovery_max_token_budget: int = 200_000
    # A second, independent consumer group on the SAME topic vectorization-service already consumes —
    # see app/sprint_recovery/kafka_trigger.py's module docstring for why this isn't new infrastructure.
    sprint_recovery_kafka_enabled: bool = True
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_content_topic: str = "jira.content.ingestion"
    sprint_recovery_kafka_group_id: str = "ai-service-sprint-recovery"
    # Off by default, same "real capability gated by config, not fake by default" shape as Phoenix
    # tracing/Kafka above. When set, escalation posts a JSON payload here (shaped as {"text": ...} so a
    # real Slack incoming-webhook URL works with zero translation) in addition to the WARNING log line
    # every deployment gets unconditionally. See app/sprint_recovery/notifications.py.
    sprint_recovery_escalation_webhook_url: Optional[str] = None

    # --- Phoenix (Arize) tracing + prompt management ---
    # Off by default: a real external service (docker run arizephoenix/phoenix), not a required
    # dependency for the service to run. See app/tracing.py for scope/coverage notes.
    phoenix_enabled: bool = False
    phoenix_collector_endpoint: str = "http://localhost:4317"
    phoenix_project_name: str = "sprint-orchestra-ai-service"


@lru_cache
def get_settings() -> Settings:
    return Settings()
