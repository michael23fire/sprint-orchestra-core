"""Runtime configuration, loaded from environment variables (12-factor).

Every value has a local-dev default so `uvicorn app.main:app` works against the Docker Compose
stack with no extra setup. Override in production via environment variables.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VEC_", env_file=".env", extra="ignore")

    # --- Service ---
    service_name: str = "vectorization-service"
    log_level: str = "INFO"
    log_json: bool = True

    # --- Postgres (this service's OWN database, with pgvector) ---
    # Not jira-backend's DB: this service keeps its own copy of the text it embeds, fed by Kafka,
    # so it never reaches into another service's tables. See docker-compose `vecdb`.
    pg_dsn: str = "postgresql://vec:vec123@localhost:5433/vecdb"
    pg_pool_min_size: int = 1
    pg_pool_max_size: int = 8

    # --- Kafka ---
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_content_topic: str = "jira.content.ingestion"
    kafka_attachment_topic: str = "jira.attachment.ingestion"
    kafka_group_id: str = "vectorization-service"
    # Start from the earliest offset the first time the group runs, so a backlog produced before the
    # consumer existed still gets ingested.
    kafka_auto_offset_reset: str = "earliest"
    # Consume attachment events too? Requires object-storage access to fetch binaries. Off by default
    # so the service runs with just issue/comment text until S3 creds are configured.
    kafka_consume_attachments: bool = False

    # --- Embeddings ---
    # Anthropic has no embeddings API (Claude is generation-only); Voyage AI is Anthropic's
    # recommended embedding provider. `openai` is also supported. See app/ingest/embedder.py.
    embedding_provider: str = "voyage"  # voyage | openai | fake
    embedding_model: str = "voyage-3"
    embedding_dim: int = 1024  # voyage-3 = 1024; text-embedding-3-small = 1536
    voyage_api_key: str = ""
    openai_api_key: str = ""
    # OpenAI-compatible base URL. Point at a local server (LM Studio / Ollama /
    # vLLM at e.g. http://localhost:1234/v1) to run real embeddings with no cloud key.
    openai_base_url: str = "https://api.openai.com/v1"
    embedding_batch_size: int = 64
    embedding_max_retries: int = 3

    # --- Chunking ---
    chunk_size_tokens: int = 500
    chunk_overlap_tokens: int = 80

    # --- Contextual retrieval (Anthropic technique: https://www.anthropic.com/news/contextual-retrieval) ---
    # Off by default — it's an LLM call per chunk, only worth the cost/latency on multi-chunk sources
    # (long attachments) where a chunk loses surrounding narrative; see app/ingest/contextualizer.py.
    contextual_retrieval_enabled: bool = False
    contextual_llm_provider: str = "anthropic"  # anthropic | openai_compatible
    contextual_anthropic_api_key: str = ""
    # Haiku, not the agent's default Opus: Anthropic's own contextual-retrieval writeup recommends a
    # small/fast model here specifically because it runs once per chunk at ingestion time, a very
    # different cost profile than the once-per-question agent loop in ai-service.
    contextual_anthropic_model: str = "claude-haiku-4-5"
    contextual_openai_base_url: str = "http://localhost:1234/v1"
    contextual_openai_api_key: str = "local"
    contextual_openai_model: str = "openai/gpt-oss-20b"

    # --- Reranking (second-stage cross-encoder over the fused candidate pool) ---
    # On by default: sentence-transformers is already a hard requirement (requirements.txt), and load
    # testing (README "Reranking" section) measured only ~10ms P50 / ~50ms P95 overhead at 50
    # concurrent requests, so there's no real cost to keep it on. See app/db/reranker.py.
    rerank_enabled: bool = True
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # First-stage candidate pool handed to the reranker before it cuts down to the caller's `limit`.
    # Must be >= the largest `limit` you expect callers to request, or reranking can't promote a
    # lower-ranked-but-more-relevant chunk into the top results.
    rerank_candidate_pool: int = 20
    # Minimum cross-encoder relevance score to keep a hit, applied after reranking and before the
    # `limit` truncation. Unlike RRF-fused scores (unitless ranks, no fixed scale — see
    # app/search/service.py's DEFAULT_MIN_SCORE docstring), a cross-encoder score is a direct
    # (query, chunk) relevance judgment, so a fixed threshold is meaningful here. `None` disables
    # filtering (rerank only reorders/truncates, same as before). ms-marco-MiniLM-L-6-v2 scores are
    # unbounded logits, not 0-1 — tune this against your own data rather than reusing a cosine-style
    # value like 0.65/0.7.
    rerank_score_threshold: float | None = None

    # --- Object storage (for attachment binaries; MinIO locally) ---
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"

    # --- Attachment parsing (Docling) ---
    docling_do_ocr: bool = False  # OCR is slow; enable when attachments contain scans/screenshots
    docling_do_table_structure: bool = False
    attachment_max_bytes: int = 25 * 1024 * 1024  # skip binaries larger than this


@lru_cache
def get_settings() -> Settings:
    return Settings()
