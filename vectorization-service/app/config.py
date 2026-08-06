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
    # 800/130 (~16% overlap), not the more common 500/80 default: a real ablation (300 vs 500 vs 800
    # tokens, all 4 RAGAS metrics, judge=qwen2.5-72b-instruct, same 19-question set) found 800 winning
    # on every metric for this corpus — see docs/RAG_ACCURACY_CASE_STUDIES.md. Attachments with tables/
    # structured data are the likely reason: they fragment badly at smaller windows.
    chunk_size_tokens: int = 800
    chunk_overlap_tokens: int = 130

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
    # EasyOCR returns a per-detection confidence (0-1) that `_ocr_image_sync` used to discard
    # entirely, joining every detection into one string regardless of confidence. Found live on a
    # synthetically degraded (rotated/blurred/low-contrast/JPEG-recompressed) test image: real words
    # ("62", "SKU", "WAREHOUSE") scored 0.7-1.0, while garbled fragments from the SAME image ("Mx",
    # "Mfate", "Eot") scored 0.006-0.06 — a clean gap, not a fuzzy boundary. Worse than noise: the
    # garbled "Mx" (the OCR's mangled read of "SKU:"+"Pallet") got confidently presented by the agent
    # as a real SKU value, not flagged as uncertain — a runtime faithfulness check can't catch this
    # class of error, because the answer WAS faithful to what got retrieved; the retrieved text itself
    # was the problem. 0.4 sits in the clean gap between this test image's garbage (<=0.34) and real
    # text (>=0.5) — not exhaustively tuned across image types, but a real improvement over "no
    # threshold at all."
    ocr_min_confidence: float = 0.4

    # --- VLM fallback for images EasyOCR struggled with (app/ingest/vlm_describer.py) ---
    # The confidence threshold above filters out garbled OCR fragments, but it can only turn "wrong"
    # into "missing" — it can't recover the value. A vision-language model sees the whole image and
    # can respond "this is illegible" instead of guessing a character sequence, which is the actual
    # gap: see docs/RAG_ACCURACY_CASE_STUDIES.md Case Study 27 (degraded-photo test) and Case Study 30
    # (this fallback's own writeup).
    #
    # Off by default — a real LLM call per escalated image, same cost argument
    # contextual_retrieval_enabled already makes. Only escalates when EasyOCR showed a concrete sign
    # of struggling (some detections dropped as low-confidence, or nothing survived at all), not run
    # unconditionally on every image.
    vlm_ocr_enabled: bool = False
    vlm_provider: str = "openai_compatible"  # anthropic | openai_compatible
    vlm_anthropic_api_key: str = ""
    vlm_anthropic_model: str = "claude-opus-4-8"  # needs vision; Haiku also supports it if cost matters more than accuracy here
    vlm_openai_base_url: str = "https://api.openai.com/v1"
    vlm_openai_api_key: str = ""
    vlm_openai_model: str = "gpt-5.6-luna"
    # A page of a scanned or visual-heavy PDF is rendered to PNG and sent to the same VLM only when
    # Docling cannot produce useful page text. This is a separate opt-in because PDF page rendering
    # can turn one attachment into several billable VLM calls.
    vlm_pdf_enabled: bool = False
    vlm_pdf_render_dpi: int = 200
    vlm_pdf_max_pages: int = 12
    vlm_pdf_min_text_chars: int = 80
    # Also inspect pages with raster/vector visuals (charts, diagrams, image-only tables), even when
    # they have some selectable text. Capped by `vlm_pdf_max_pages` above.
    vlm_pdf_include_visual_pages: bool = True
    vlm_max_completion_tokens: int = 2_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
