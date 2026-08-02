"""Runtime configuration for the ai-service (query-time agent layer)."""
from __future__ import annotations

from functools import lru_cache

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

    # --- Phoenix (Arize) tracing + prompt management ---
    # Off by default: a real external service (docker run arizephoenix/phoenix), not a required
    # dependency for the service to run. See app/tracing.py for scope/coverage notes.
    phoenix_enabled: bool = False
    phoenix_collector_endpoint: str = "http://localhost:4317"
    phoenix_project_name: str = "sprint-orchestra-ai-service"


@lru_cache
def get_settings() -> Settings:
    return Settings()
