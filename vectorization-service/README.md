# vectorization-service

RAG service for Sprint Orchestra. It owns the index: it consumes issue / comment / attachment change
events from Kafka, turns their text into chunks, embeds them, and upserts into its own **pgvector**
database — and it owns the **raw retrieval API** (`POST /search`) over that same index, combining
dense (vector) and lexical (Postgres FTS on the chunk text) search via Reciprocal Rank Fusion.

What it deliberately does *not* own: query-time business logic — permission resolution beyond the
`space_ids` a caller passes in, answer synthesis, agentic re-retrieval, citation formatting for a chat
UI. That composition is [`ai-service`](../ai-service)'s job, which calls `/search` as a tool. Same
split as jira-backend's own FTS: the service that owns the data exposes primitives; the service that
owns the product feature composes them.

Note this is a *different* FTS index than jira-backend's: jira-backend's `tsvector` lives on the
*original* `issues`/`comments` rows and is maintained by Postgres for free on write — duplicating that
here would buy nothing. This service's `tsvector` lives on **chunks** (which also cover attachment
text jira-backend has no FTS over at all) specifically so a lexical hit and a vector hit can be fused
rank-for-rank over the same candidate set — a different, RAG-specific need, not a duplicate.

```
jira-backend ──(AFTER_COMMIT)──▶ Kafka: jira.content.ingestion ──▶ vectorization-service
  create/update/delete issue                jira.attachment.ingestion       │
  create/update/delete comment                                              ├─ HTML→text / Docling
  upload/delete attachment                                                  ├─ [contextualize] (opt.)
                                                                            ├─ chunk (token+overlap)
                                                                            ├─ embed (Voyage/OpenAI)
                                                                            └─ upsert → pgvector
                                                                                        ▲
                                                          POST /search (vector ∪ lexical, RRF) ──▶ ai-service
```

## Why event-driven, and why these events

- **Every content change is covered, not just attachments.** jira-backend now emits
  `issue_upserted` / `issue_deleted` / `comment_upserted` / `comment_deleted` alongside the existing
  attachment events. Ingesting only attachments would miss the ~90% of issues that have none.
- **Deletes are explicit.** The vector store is a *separate copy* of the text, so Postgres cascade
  deletes never reach it. Without delete events, a removed comment would keep surfacing in semantic
  search (stale index). `issue_deleted` purges an issue's issue/comment/attachment vectors in one go.
- **Upsert on a deterministic key**, never similarity search. Chunk ids are `issue:{id}`,
  `comment:{id}`, `attachment:{id}#{index}`, so re-embedding overwrites in place instead of
  accumulating stale duplicates. This is what makes Kafka's at-least-once delivery safe.
- **Kafka absorbs the latency gap.** jira-backend returns as soon as it publishes; the embedding
  work happens here, on this service's own schedule, without holding the user's save hostage.

## Chunking & attachment handling

- **Chunk granularity:** issue title+description = one document; each comment = its own chunk; each
  attachment's extracted text = its own chunk(s). Long text is split into ~500-token windows with
  ~80-token overlap so a sentence on a boundary still appears whole somewhere.
- **HTML** (issue/comment rich text) is stripped to plain text before chunking; inline `<img>` markers
  are dropped (the image is embedded separately as an attachment chunk).
- **Attachments** are parsed with **Docling** → `export_to_markdown()` for PDF/DOCX/PPTX/XLSX/MD,
  which preserves reading order and renders tables as Markdown in place. Two formats bypass Docling
  entirely, both found live against a real 132-file attachment corpus, not by inspection:
  - **CSV / `.txt` / `.log`** — Docling 2.14 has no `InputFormat` for plain text at all and hard-fails
    on it; these are just decoded directly instead.
  - **Standalone images (`.png`/`.jpg`)** — Docling's own document-layout classifier was found to
    treat a whole screenshot's text content as an un-OCR'd "Picture" region (recovering only a stray
    header fragment), regardless of `do_ocr`/`force_full_page_ocr`/upscaling. Fixed by running
    **EasyOCR directly** on the full image canvas — the same OCR engine Docling itself uses, just
    without the layout-classification step that was deciding not to run it — sorted into approximate
    reading order (row-clustered by vertical position, left-to-right within a row). Verified live:
    went from recovering ~10% of a screenshot's text to essentially all of it.
  A third bug (an exception-logging call that crashed on `filename` colliding with a reserved Python
  `LogRecord` attribute, masking the real error) was found in the same pass — see
  `app/ingest/docling_parser.py`'s module docstring for the full story on all three.
  `scripts/backfill_jira_backend.py --include-attachments` is the tool that surfaced all of this — a
  general-purpose backfill for indexing jira-backend content that predates Kafka publishing being
  enabled, reused here as an attachment-pipeline test harness against real data. OCR and
  table-structure models are **off by default** (`VEC_DOCLING_DO_OCR`/`VEC_DOCLING_DO_TABLE_STRUCTURE`)
  — a real per-file cost, not free, so opt in per deployment.
- **Metadata on every chunk** — `space_id` (for the same per-space permission filter the FTS path
  uses), `chunk_type`, `source_id`, `issue_key` — so a retrieval hit can be filtered by access and
  linked back to a clickable source.

## Hybrid retrieval (dense + lexical, fused with RRF)

`POST /search` supports three modes:

| mode      | what it does |
|-----------|--------------|
| `vector`  | cosine similarity over embeddings |
| `lexical` | Postgres `ts_rank_cd` over a `tsvector` generated column on `chunks.content` |
| `hybrid` (default) | both, fused via **Reciprocal Rank Fusion** (`app/db/rrf.py`) |

RRF combines *ranks*, not raw scores — cosine distance and `ts_rank_cd` live on unrelated scales, so
blending them directly would just be an unexplainable magic-weight formula (same reasoning as the
tiered ranking in jira-backend's own `SearchService`). A chunk both retrievers agree on gets the sum
of both reciprocal-rank contributions, so agreement between signals outranks a single retriever's #1
pick — see `tests/test_rrf.py` for the worked-out arithmetic. This is real, measurable resilience, not
just a design story: in testing, a query with extra words the lexical index couldn't match (strict-AND
`plainto_tsquery` found zero hits) was still answered correctly because vector search alone ranked the
right chunk #1 — hybrid absorbs a single retriever's blind spot.

## Contextual retrieval (optional, off by default)

[Anthropic's technique](https://www.anthropic.com/news/contextual-retrieval): before embedding a
chunk from a **multi-chunk** source, ask a small/fast LLM to write one sentence situating that chunk
within its source document, and prepend it — so a chunk like *"raising the pool size and lowering
max-lifetime resolved it"*, which means nothing in isolation, gets embedded (and lexically indexed,
and cited) alongside a sentence identifying which incident and which system it's about.

Single-chunk sources (a typical issue or comment) skip this entirely — there's nothing to "situate"
a chunk within when it already *is* the whole document; the LLM call would just restate what's
there. It only fires on longer attachments split into multiple chunks. Off by default
(`VEC_CONTEXTUAL_RETRIEVAL_ENABLED=false`): it's a real LLM call per chunk, a genuine ingestion
cost/latency tradeoff, not something to enable silently. See `app/ingest/contextualizer.py`.

## Structured issue queries (`POST /issues/query`) — the counting/filtering path

Semantic search answers "what content is relevant"; it structurally **cannot** answer "how many bug
issues are there", "list every story", "which issues are blocked", or "what changed most recently".
Those are exact/aggregate questions over structured metadata, and a top-K similarity retriever returns
its best *guesses*, never a complete, counted set — so asking it to count produces a confident
undercount (observed live: the agent, with only semantic search, answered "several bugs, maybe around
10" when the real count was 11).

So ingestion now has a **structured half** alongside the embedding half. Every `issue_upserted` event
also upserts a row into an `issues` table (`migrations/004_issues_metadata.sql`) holding
`issue_type`, `status`, `title`, and the issue's own `created_at`/`updated_at`. This is a separate
table from `chunks` on purpose: one row per issue regardless of how its text chunks (so `COUNT` is
exact), and an issue with an empty description — which embeds to zero chunks — still gets recorded and
counted. `POST /issues/query` runs plain, deterministic SQL over it: space-scoped (same permission
boundary as `/search`), filterable by type/status/created/updated ranges, ordered by either timestamp,
returning an **exact `total_count`, a `counts_by_type`/`counts_by_status` breakdown, and an ordered,
limited sample of matching issues**. The ai-service agent gets this as a second tool (`query_issues`)
and routes count/filter/recency questions to it — see that service's README.

```bash
# how many bugs, and their status breakdown, in space 5000014:
curl -s localhost:8100/issues/query -H 'content-type: application/json' \
  -d '{"space_ids":[5000014],"issue_types":["bug"]}' | jq '{total_count, counts_by_status}'
# → {"total_count": 11, "counts_by_status": {"done": 11}}
```

jira-backend's producer emits all four metadata fields on every issue upsert
(`IssueContentChangedEvent` → `IssueIngestionMessage`); they remain `Optional` on the consumer's
model only so pre-upgrade messages still parse during a rolling deploy.

## Issue change history (`POST /issues/history`) — transitions, not state

The `issues` snapshot is an upsert — which is exactly what destroys transitions: once a status is
overwritten, "was this ever reopened?" is unanswerable from latest state. So changes get their own
**append-only** stream. jira-backend already records every field change (old → new value, including
title/description edits) in its `issue_history` table; each recorded row is now bridged to Kafka as an
`issue_history_added` event and lands here in `issue_changes` (`migrations/005`), keyed on the
*upstream* history id so at-least-once delivery and backfill overlap are `ON CONFLICT DO NOTHING`
no-ops. This service never diffs anything itself — the system of record for "what changed" is
upstream, and duplicating diff logic downstream would just let the two disagree.

Two things ride on this stream:

- **`POST /issues/history`** — space-scoped query with `issue_keys` / `fields` / `event_types` /
  `since` / `until` filters plus a first-class `reopened_only` (status `field_change` leaving `done` —
  the transition question people actually ask, not expressible via the generic filters). Exact
  `total_count` + newest-first sample, same honesty contract as `/issues/query`. The ai-service agent
  exposes it as the `get_issue_history` tool ("which issues were reopened", "what did I just change in
  ATC-77").
- **Snapshot freshness for metadata-only edits.** jira-backend deliberately does NOT fire a content
  event for a status-only edit (no text changed → no re-embed), so the history event is the only way
  the `issues` row learns about it. The consumer applies status/issueType `field_change`s to the
  snapshot (first insert only — a redelivered stale event can't regress a newer status), keeping
  "which issues are blocked" current without wasting embedding calls.

## Reranking (on by default)

`POST /search` runs a second-stage **cross-encoder reranker** over the fused candidate pool before
truncating to `limit` — i.e. hybrid RRF fetches a wider pool (`VEC_RERANK_CANDIDATE_POOL`, default 20),
the cross-encoder rescores it, and the response is the reranker's top `limit` (default 5-10 as set by
the caller). Hybrid RRF fusion combines *ranks* from two retrievers that never looked at query and
chunk together; a cross-encoder takes `(query, chunk)` as one joint input and scores relevance
directly — strictly more informative, at real per-request latency cost, which is exactly why it's a
second stage over a small candidate pool rather than running over the whole index. See
`app/db/reranker.py`.

`sentence-transformers` (`cross-encoder/ms-marco-MiniLM-L-6-v2`) is a hard dependency
(`requirements.txt`), loaded once at startup, so there's no install-size reason to keep this off; set
`VEC_RERANK_ENABLED=false` to fall back to plain hybrid RRF with no second stage.

**Score threshold.** `VEC_RERANK_SCORE_THRESHOLD` (unset by default) drops hits below that
cross-encoder score *before* truncating to `limit`, so a query with no genuinely relevant chunks in
the candidate pool can return fewer than `limit` hits — even zero — instead of always padding out to
`limit` with weak matches. This is deliberately different from `DEFAULT_MIN_SCORE=0.7` in
`ai-service/app/search/service.py` (a *different* feature, issue dedup, that thresholds raw vector
cosine scores): the cross-encoder's score scale is model-specific unbounded logits, not 0-1 cosine, so
don't reuse a cosine-style threshold like 0.65/0.7 here — tune it empirically against `eval/` or real
queries. An empty result list is not an error: the ai-service CRAG loop
(`ai-service/app/agent/crag_loop.py`) already treats "no results" as a signal to reformulate the query
and search again, so filtering to empty here composes naturally with that behavior rather than needing
special-case handling.

**Measured** (50 concurrent requests, `loadtest/locustfile_vectorization.py` — see
`../loadtest/README.md` for the full methodology): reranking on vs. off moved P50 latency by only
~10ms (620ms vs. 610ms) and P95 by ~50ms (820ms vs. 770ms) at this load — the cross-encoder is not the
bottleneck; the shared `asyncpg` connection pool (`VEC_PG_POOL_MAX_SIZE`, default 8) is the more likely
next tuning lever if latency needs to come down further, since 50 concurrent requests sharing 8 pooled
connections queue for a connection before the reranker (or the database) ever runs.

## Embeddings

Anthropic has no embeddings API (Claude is generation-only); **Voyage AI** is Anthropic's
recommended embedding provider and the default here. `openai` is supported, and a `fake`
deterministic embedder lets the service and tests run with no key or network.

| Provider | `VEC_EMBEDDING_MODEL` | `VEC_EMBEDDING_DIM` |
|----------|-----------------------|---------------------|
| voyage   | `voyage-3`            | 1024                |
| openai   | `text-embedding-3-small` | 1536             |
| fake     | (any)                 | any                 |

Changing the dimension requires updating the `vector(N)` column in `migrations/001_create_chunks.sql`.

## Run locally

```bash
# 1. Infra (from repo root) — brings up Kafka, MinIO, and the pgvector db `vecdb` on :5433
docker compose up -d vecdb kafka zookeeper

# 2. Enable the producers in jira-backend (so it emits the events)
export APP_KAFKA_CONTENT_INGESTION_ENABLED=true
export APP_KAFKA_ATTACHMENT_INGESTION_ENABLED=true   # optional
./gradlew :jira-backend:bootRun

# 3. This service
cd vectorization-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # set VEC_VOYAGE_API_KEY, or VEC_EMBEDDING_PROVIDER=fake to run without a key
uvicorn app.main:app --port 8100
```

Create/edit an issue in the app, then:

```bash
curl localhost:8100/stats     # processed count + vector_count climbs
curl localhost:8100/healthz
```

## Endpoints

| Method | Path       | Purpose |
|--------|------------|---------|
| GET    | `/healthz` | Liveness + DB ping + consumer status (Docker/K8s probe) |
| GET    | `/stats`   | Processed / skipped / failed counters + current vector count |
| GET    | `/metrics` | Prometheus text-format metrics (request latency, per-stage search timings) — see Observability below |
| POST   | `/search`  | Raw retrieval: `{query, space_ids, limit, mode}` → ranked chunk hits with citation metadata |
| POST   | `/embed`   | Raw embedding primitive: `{text}` → `{embedding, dim}`. Exposed so `ai-service`'s semantic query cache can embed questions without duplicating an embedding provider — see `ai-service/app/cache/`. |

## Observability

`GET /metrics` (Prometheus text format) exposes request-level latency (`http_request_duration_seconds`,
labeled by method+path) and, specifically for `/search`, per-stage histograms —
`search_embed_seconds`, `search_retrieval_seconds` (labeled by `mode`), `search_rerank_seconds` — so
"is this slow, and *which stage*" is a metrics query, not a log grep. Every request also gets an id
(`app/observability.py`, propagated via the `x-request-id` response header and a `ContextVar`), logged
on the `"search completed"` structured log line alongside a `stage_ms` breakdown — the metric and the
log are measured from the same timer, so they can't disagree.

Point a local Prometheus at `http://localhost:8100/metrics` (and `ai-service` at `:8200/metrics`) to
graph these; **not** wired up as full distributed tracing (OpenTelemetry spans across the
`ai-service → vectorization-service` call chain) — that's the natural next step for a multi-service
call chain, noted here rather than silently absent.

## Retrieval evaluation

RAG quality is measured, not asserted. [`eval/`](eval/) holds a labeled benchmark — the **AtlasCart**
engineering workspace ([`eval/dataset.py`](eval/dataset.py): 22 issues + 7 comments) plus 24 queries
modeling the classic industry use cases a RAG/agent over a Jira workspace must handle:

- **root-cause** — *"why did the payment checkout fall over during the holiday rush"* → the HikariCP
  pool-exhaustion comment (near-zero keyword overlap with the answer)
- **how-did-we-fix** — *"how did we stop customers from being billed more than once"* → the
  idempotency-key comment
- **feature intent in user language** — *"make the site easier on the eyes at night"* → the dark-mode
  issue
- **lexical** — exact ticket keys (`ATLAS-6`, `ATLAS-13`, `ATLAS-20`) and rare tokens (`HikariCP`, a
  `429` error code) — included **honestly** to show where dense retrieval structurally fails
- **distractor-aware semantic** — ATLAS-13/14/15/17/18/19/21 are each a near-neighbor of a *different*
  correct answer (same topic area, different specific bug — e.g. two separate "checkout" issues), and
  comment:207 shares the literal token "HikariCP" with the true root-cause comment without being the
  fix. These exist because the original 12-issue corpus was small enough that almost any retriever hit
  the top 3 trivially; the distractors force retrieval to discriminate on more than topic/vibe. See the
  module docstring in `eval/dataset.py` for the full list.

`eval/run_eval.py` runs the *real* ingestion path (chunk → embed → pgvector), then queries it through
the **same production code** the ai-service tool call and `POST /search` use —
`VectorStore.search_vector` / `search_lexical` / `search_hybrid` and `CrossEncoderReranker` — and
reports Hit@k/Recall@k/MRR (`eval/metrics.py`) for all four modes: vector-only, lexical-only, hybrid
(RRF, no rerank), and hybrid+rerank. An earlier version of this script hand-rolled its own raw SQL
cosine query, which meant it only ever measured the vector-only path and never actually exercised
hybrid fusion or reranking, even after both shipped — worth naming since it's an easy trap (an eval
harness silently drifting out of sync with the production code path it's supposed to be measuring).

```bash
# with the vecdb container up and an OpenAI-compatible embedder (e.g. local LM Studio):
VEC_EMBEDDING_PROVIDER=openai VEC_OPENAI_BASE_URL=http://localhost:1234/v1 \
VEC_EMBEDDING_MODEL=text-embedding-qwen3-0.6b-text-embedding VEC_EMBEDDING_DIM=1024 \
VEC_PG_DSN=postgresql://vec:vec123@localhost:5433/vecdb \
python -m eval.run_eval
```

**Measured result** (`text-embedding-qwen3-0.6b`, local via LM Studio, 1024-dim; current 22-issue/7-
comment corpus, 24 queries; all four modes run through the actual `VectorStore`/`CrossEncoderReranker`
production code, not a stand-in):

| mode | Overall MRR | Hit@1 | Hit@3 | Semantic MRR | Lexical MRR |
|------|------------:|------:|------:|--------------:|------------:|
| vector-only | 0.812 | 0.750 | 0.917 | 0.939 | 0.333 |
| lexical-only (FTS) | 0.208 | 0.208 | 0.208 | 0.053 | 0.800 |
| hybrid (RRF, no rerank) | 0.910 | 0.833 | **1.000** | 0.939 | 0.800 |
| **hybrid + cross-encoder rerank** | **0.979** | **0.958** | **1.000** | **1.000** | **0.900** |

Reading: vector-only misses both opaque-identifier queries (`ATLAS-6`, `ATLAS-13` — not in its top 3
at all), and lexical-only misses nearly every semantic query (MRR 0.053) — each tier's documented
weakness, reproduced with numbers, not just asserted. Hybrid RRF fusion covers both: Hit@3 reaches
**1.000**, zero misses across all 24 queries. Reranking on top of that pushes Hit@1 from 0.833 to
0.958 — the correct answer is now usually rank 1, not just "somewhere in the top 3" — and lexical MRR
*improves* from 0.800 to 0.900 rather than regressing, confirming the `issue_key`-in-reranker fix below
holds under real embeddings, not just the synthetic run that first caught it.

**This gap used to be real, then a bug, now it's actually closed** — worth being explicit about,
since it was found and fixed via live testing, not caught in review: migration `002` originally built
`search_vector` from `content` only, which does **not** contain the issue's own key text (`content` is
the chunk's prose, not its metadata) — so a lexical query for `"ATLAS-6"` matched **zero rows**, silently
failing to deliver on the "lexical tier handles exact keys" claim this README made. Migration `003`
fixes this (`search_vector` now indexes `issue_key || ' ' || content`), verified live:
`mode=lexical` now returns `ATLAS-6` directly, and `mode=hybrid` ranks it #1 (fused from both
`lexical` and `vector` agreeing — see `retrievers: ["lexical", "vector"]` in the response). Existing
data needs migration `003` applied (`docker exec -i <vecdb-container> psql -U vec -d vecdb <
migrations/003_lexical_index_includes_issue_key.sql`); fresh installs get it automatically.

**Same shape of bug, one layer up, caught by this eval expansion.** `CrossEncoderReranker` had the
identical mistake `search_vector` did before migration 003: it scored `(query, hit.content)` pairs
only, and `content` never contains the chunk's own issue key (`app/ingest/pipeline.py` stores them as
separate fields). Running the four-mode comparison above (with a deterministic fake embedder, so only
the reranker's contribution is being isolated) showed it live: lexical-slice MRR was **0.700** under
plain hybrid RRF and dropped to **0.251** after reranking — the cross-encoder was actively *demoting*
chunks that FTS had already correctly found, because it had no way to see the identifier the query was
literally asking for. Fixed by scoring `(query, f"{hit.issue_key} {hit.content}")` instead
(`app/db/reranker.py`); re-running confirmed lexical MRR back at 0.700 (no regression vs. hybrid alone)
with semantic MRR up to 0.747 (rerank's real contribution, on top of the fake vector stage). Kept
non-fatal in this codebase because unit tests were content-only and never exercised an identifier
query — a good argument for eval queries and unit test fixtures to intentionally include the same
"opaque identifier" edge case, not just prose.

## Tests

```bash
pip install -r requirements.txt
pytest        # chunker, RRF fusion, pipeline idempotency, contextual retrieval wiring, eval metrics
              # — all hermetic (fake embedder / in-memory store / fake context generator, no infra)
```

## Known simplifications (portfolio scope)

- **At-most-once publish in jira-backend.** Events use `@TransactionalEventListener(AFTER_COMMIT)`;
  a crash between commit and Kafka publish loses the event. Production would use a transactional
  outbox or Debezium CDC for at-least-once. The consumer side here is already at-least-once.
- **No dead-letter topic.** Structurally-bad messages are logged and skipped so they don't wedge a
  partition; transient failures are redelivered. A DLQ would be the next step.
- **Answer synthesis and agentic re-retrieval are out of scope** — this service exposes the raw
  `/search` primitive; composing it into an answer with citations, and deciding when to re-retrieve,
  is [`ai-service`](../ai-service)'s job.
- **Reranker scores are raw cross-encoder logits, not normalized to `[0, 1]`** — when
  `VEC_RERANK_ENABLED=true`, `SearchHit.score` in the response is whatever `ms-marco-MiniLM-L-6-v2`
  outputs (commonly negative), not a probability. Fine for *ranking* (higher is more relevant), not
  meaningful as an absolute confidence number — don't threshold on it without recalibrating.
- **50-concurrent-user load test passed with zero failures** (`../loadtest/README.md`) — the one claim
  in this README backed by an adversarial test specifically designed to break it, not just normal
  usage during development.
