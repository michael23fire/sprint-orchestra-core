# ai-service

The **query-time agent layer** for Sprint Orchestra's RAG stack. It answers natural-language
questions about the workspace using an **agentic RAG loop with corrective retrieval**: Claude (via
tool use) searches [`vectorization-service`](../vectorization-service)'s hybrid index, assesses
whether what came back actually answers the question, and — if not — reformulates and searches
again, up to a bounded number of rounds, before either answering with citations or honestly saying it
doesn't have enough information.

```
User question ──▶ ai-service (CragAgent)
                     │
                     │ 1. search_knowledge_base(query, mode?)  ──▶  POST /search (vectorization-service)
                     │ 2. assess: does this answer the question?
                     │      insufficient? → reformulate, go to 1 (bounded by AI_MAX_TOOL_ITERATIONS)
                     │      sufficient?   → answer, with citations, or abstain honestly
                     ▼
              {answer, citations, retrieval_rounds, queries_used, abstained}
```

## Why "corrective" retrieval, and why it's the model's own reasoning

Standard RAG tool use is one retrieval, then answer — it trusts the first search blindly. **Corrective
RAG** (Yan et al., 2024) doesn't: it grades retrieval quality and re-retrieves when it's poor. The
original paper trains a small classifier for that grading step. This implementation makes a
deliberate simplification: the **agent model grades its own retrieval**, as part of the same tool-use
loop, instead of a separate trained component or an extra round-trip call. Re-retrieval is just
another tool call the loop already supports, triggered by the model's own judgment via the system
prompt (`app/agent/crag_loop.py`) rather than a fixed pipeline stage. This is how corrective retrieval
is commonly implemented in production agentic systems today — the grading step is "a capable model's
judgment," not a bespoke classifier — and it was **proven working against a real model**, not just
designed on paper (see Results below: query reformulation firing mid-conversation, e.g. `"vague
query"` → `"HikariCP connection pool"`).

## Seven tools: semantic content, exact state, and transitions

The agent has **seven** tools, and picking the right one is the point:

- `search_knowledge_base` — semantic/keyword search over issue and comment **text**. For content
  questions: "why did X break", "how did we fix Y", "find issues about topic T".
- `query_issues` — a structured lookup over issue **metadata** (type, status, timestamps), backed by
  vectorization-service's `POST /issues/query`. For counting, listing, filtering, and recency: "how
  many bugs", "list the stories", "how many issues in total", "which are blocked", "what changed most
  recently".
- `get_issue_history` — the append-only **change log** (who changed what, when, old → new value,
  including title/description edits), backed by `POST /issues/history`. For transition questions the
  other two structurally can't answer: "which issues were REOPENED" (the snapshot only knows current
  status — the reopen transition was overwritten), "what did I just change in ATC-77", "who moved X
  to blocked". Because relative time ("just now", "past 3 months") is meaningless to a model without
  a clock, the system prompt is built per-request with the current UTC timestamp so the model can
  resolve those into concrete `since` filters.
- `query_sprints` — exact sprint state, ordering, dates, and issue counts; used before issue queries
  when a question refers to an active or named sprint.
- `get_issue_comments`, `get_issue_details`, and `get_issue_attachments` — deterministic fallback
  reads for known issue keys when top-k semantic search is the wrong way to fetch complete source
  material. These paths return citation-bearing evidence, not model memory.

This exists because semantic retrieval **cannot answer a "how many" or "list all" question** — it
returns its top few best guesses, never a complete, counted set. Live, with only semantic search, the
agent answered "several bugs, roughly 10" to a question whose exact answer was 11; with `query_issues`
it now returns the exact count, a by-type/by-status breakdown, and the full list, and even corrects a
wrong guess in the question. The system prompt (`app/agent/crag_loop.py`) teaches the routing, and a
question that mixes both ("how many bugs are about checkout") uses `query_issues` to filter by type and
`search_knowledge_base` for the topic. `query_issues` returning a count of **0** is a real answer
("there are none"), explicitly not a reason to abstain.

The same `space_ids` security boundary below applies to *all seven* tools: the loop injects the
caller's authorized spaces into every structured call and strips any non-whitelisted field (including
a model-supplied `space_ids`) from the tool input — a prompt-injected model cannot widen its own scope
through the structured paths any more than through search.

## Security: the model never controls `space_ids`

The `search_knowledge_base` tool the agent can call has exactly one input the model controls: `query`
(and optionally `mode`). It is **never given `space_ids`**. Permission scoping is injected by the loop
from the caller's authenticated identity on *every single* search call, regardless of what the model
puts in its tool arguments — mirroring the same per-space boundary jira-backend's FTS enforces
(`SpaceMemberRepository.findActiveSpaceIdsByUserId`). An LLM must never be trusted with an
authorization parameter: a confused or prompt-injected agent could otherwise be talked into searching
spaces the calling user has no access to. The boundary is enforced by code the model cannot influence
— see `tests/test_crag_loop.py::test_space_ids_are_never_taken_from_the_model`.

## Running against Claude vs. running locally

Two `LLMClient` implementations (`app/llm/`) sit behind one interface the agent loop is written
against once:

| `AI_LLM_PROVIDER` | Implementation | When |
|---|---|---|
| `anthropic` (default) | Official `anthropic` SDK, `claude-opus-4-8` | **Production.** Needs `ANTHROPIC_API_KEY` (or `ant auth login`). |
| `openai_compatible` | Raw HTTP to an OpenAI-compatible server (LM Studio / Ollama / vLLM) | **Local dev/testing only**, no cloud key. This is how the loop's control flow (does it re-retrieve? does it respect the space_ids boundary? does it abstain correctly?) was actually developed and verified in this repo — against a real local model (`openai/gpt-oss-20b`), not a mock. |

The Anthropic path is a near-pass-through of the SDK. The OpenAI-compatible path is the one place
that translates between Anthropic's native message/tool-call shape (which the agent loop is written
against, since that's the production target) and OpenAI's different shape — see
`app/llm/openai_compatible_client.py`. That translation complexity is isolated to the local-testing
path and never leaks into `crag_loop.py`.

```bash
cd ai-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Local, no key (matches how this was developed):
# AI_LLM_PROVIDER=openai_compatible, AI_AGENT_MODEL=qwen/qwen3.6-35b-a3b, AI_OPENAI_BASE_URL=http://localhost:1234/v1

uvicorn app.main:app --port 8200
```

Requires `vectorization-service` (`:8100`) with an ingested corpus. The default configuration also
enables Redis semantic caching, both Postgres-checkpointed workflows, and the sprint-recovery Kafka
trigger, so Redis (`:6379`), core Postgres (`:5432`), and Kafka (`:9092`) must be running. For a
minimal standalone process, set `AI_CACHE_ENABLED=false`, `AI_EPIC_ROLLOUT_ENABLED=false`, and
`AI_SPRINT_RECOVERY_ENABLED=false`.

### Optional: routing through the LiteLLM gateway instead of directly to a provider

`../litellm-gateway/` runs an [LiteLLM Proxy](https://docs.litellm.ai/docs/simple_proxy) in front of
both backends above, adding centralized **rate limiting and per-key spend budgets** — governance this
service's own `LLMClient` abstraction and cost tracking (`app/stats.py`) deliberately don't do
themselves (that measures spend; it never stops it). Because `OpenAICompatibleClient` already speaks
plain OpenAI-compatible HTTP, pointing it at the gateway instead of a provider directly is a
**config-only** change — no code in this service changes:

```bash
AI_LLM_PROVIDER=openai_compatible AI_AGENT_MODEL=local-chat \
AI_OPENAI_BASE_URL=http://localhost:4000 AI_OPENAI_API_KEY=<a litellm virtual key> \
uvicorn app.main:app --port 8200
```

Verified live: full `/ask` corrective-RAG loop (retrieval, tool use, grounded answer) run end to end
through the gateway; a rate-limited virtual key correctly 429'd on the 3rd request within its window;
a budget-capped virtual key correctly 429'd once spend crossed its cap. See `../litellm-gateway/README.md`
for the exact commands and real output.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | Liveness |
| GET | `/stats` | Cumulative token usage + estimated $ cost since process start — see Cost tracking below |
| GET | `/metrics` | Prometheus text-format metrics (request latency, LLM/retrieval/cache stage timings, corrective-retrieval-rounds distribution) — see Observability below |
| POST | `/ask` | `{question, space_ids}` → `{answer, abstained, retrieval_rounds, queries_used, citations, input_tokens, output_tokens, estimated_cost_usd, cache_hit}` |
| POST | `/ask/stream` | SSE progress plus the same final Ask result |
| POST | `/search` | Space-authorized semantic issue dedup/search helper |
| POST | `/draft-task` | `{description, existing_labels?}` → `{draft: {title, issue_type, labels, estimate_story_points, dependencies}, degraded, latency_seconds}` — see "AI-assisted task creation" below |
| POST | `/plan-epic`, `/plan-epic/refine` | Generate or refine a structured epic plan; optional planner↔critic LangGraph path |
| POST/GET | `/plan-epic/rollout/**` | Start/stream/read/approve/reject/retry a durable human-approved rollout |
| POST | `/sprint-health` | Deterministic sprint risk signals plus grounded AI analysis |
| POST/GET | `/sprint-recovery/**` | Durable recovery start, clarification, decision, retry, reevaluation, history, and time travel |

## Cost tracking

Every LLM call's token usage (`app/llm/types.py::Usage`) is threaded through the whole CRAG loop —
including every corrective-retrieval round and the forced-final-answer call — and summed per request.
`app/llm/pricing.py` holds a static $/1M-token table (Anthropic's published pricing) and turns that sum
into `estimated_cost_usd` on every `/ask` response; `GET /stats` accumulates it across the process's
lifetime. Local/OpenAI-compatible models (LM Studio, etc.) price at **$0** — there's no metered API
bill for them, though token counts are still tracked for capacity-planning visibility. This answers "how
much did this demo actually cost me" with a number, not a guess — see
[`docs/AWS_DEPLOYMENT.md`](../docs/AWS_DEPLOYMENT.md) for realistic total cost estimates of running
this project's cloud path.

## Semantic query cache (on by default), backed by Redis Stack

`POST /ask` checks a two-tier cache (`app/cache/semantic_cache.py`) before running the full agent loop:
an **exact-match** tier (a single Redis `GET`) and a **semantic** tier (cosine similarity over question
embeddings — fetched via `vectorization-service`'s `POST /embed`, reusing its embedder rather than
duplicating one here) for paraphrases the exact tier misses. A cache hit skips retrieval, the
corrective-retrieval loop, and generation entirely.

The semantic tier runs as a **native Redis Stack vector KNN search** (`FT.SEARCH ... KNN`, RediSearch
module), not a linear scan in Python — an earlier version pulled every candidate embedding into Python
and computed cosine similarity by hand, an O(n) scaling limit this version removes. Verified live
against a real `redis-stack-server` container (not assumed from docs): RediSearch's `COSINE` distance
metric returns *distance* (0 = identical), not similarity, so `similarity = 1 - distance`; and a
`space_key` `TAG` filter combined with `KNN` in the same query genuinely returns zero results for a
different scope, confirmed directly. **Considered and rejected**: swapping this hand-rolled cache for
the `GPTCache` library — functionally equivalent to what's already here, and trading an implementation
that's fully understood and tested for a less transparent one isn't worth it for name-recognition
alone; documented as a deliberate trade-off, not an oversight.

Two things this design is deliberately strict about, because getting either wrong means silently
serving a **wrong** answer, which is worse than a cache miss:
- **Never crosses `space_ids` scopes** — a cached answer computed for one permission scope can never
  be served to a different one, even for byte-identical question text. Tested explicitly
  (`tests/test_semantic_cache.py::test_space_ids_isolation_never_leaks_across_permission_scopes`).
- **A deliberately high similarity threshold** (`AI_CACHE_SIMILARITY_THRESHOLD`, default `0.97`) — a
  cache miss just costs one extra LLM call; a false-positive hit returns a wrong answer to the user.
  **Measured, not assumed**: two real paraphrases of the same question ("why did checkout fail during
  the holiday sale" vs. "what caused the checkout process to break during the holiday sale event"),
  embedded with a real local embedding model, scored **0.92 cosine similarity** — below the 0.97
  default, so this pair correctly falls through to a fresh LLM call rather than a risky semantic hit.
  That's the actual tradeoff this default makes, quantified: conservative on purpose, tunable via
  `AI_CACHE_SIMILARITY_THRESHOLD` if you want more hits at the cost of more false-positive risk.

**Measured** (live, same corpus/model as above): an exact-match hit returned in **16ms**, vs. **18.7s**
for the original uncached request — the actual latency/cost this cache buys, not a theoretical claim.

Known simplification: invalidation is **TTL-only** (`AI_CACHE_TTL_SECONDS`, default 600s), not
event-driven — the system already has a natural invalidation signal (the same Kafka content-change
events `vectorization-service` consumes) that a production version would use to purge affected entries
on write. Cache state is already shared in Redis Stack and uses native RediSearch KNN; it is not an
in-process dictionary.

## AI-assisted task creation (`POST /draft-task`)

One structured-output LLM call turns a rough, informal description into a validated draft:
title / issue type / labels / estimate / dependencies. Built with:

- **[Instructor](https://python.useinstructor.com/) + Pydantic** (`app/drafting/schemas.py`,
  `app/drafting/service.py`) — the response is validated against a `TaskDraft` Pydantic model, with
  automatic re-prompt-and-retry on a validation failure. Kept separate from `app/llm/factory.py`'s
  hand-rolled `LLMClient` (which the CRAG agent loop needs direct control over `stop_reason`/tool
  calls for): this feature has no control flow to inspect, just one call in, one validated object
  out — exactly what `instructor` is for, so it isn't reimplemented by hand a second time.
- **Jinja2-templated prompt** (`app/drafting/templates/task_draft_system.jinja`) — has a genuine
  conditional section (whether to list `existing_labels` to prefer reusing), which Jinja2's `{% if %}`
  expresses directly; the prompt wording also lives in its own file, separate from and diff-able apart
  from the Python code that calls it.
- **Degradation strategy** (`app/drafting/service.py::_fallback_draft`) — if `instructor`'s own
  retries are exhausted, the endpoint never 500s: it returns a safe, always-valid `TaskDraft` built
  from the raw description (first line as the title, everything else empty/null) with `degraded=true`,
  so the caller can tell "AI drafted this" from "AI failed, here's an unassisted starting point" — a
  degraded draft is still a usable task, never a blocked user.

**Mode selection was found by testing against this project's actual local model, not assumed from
docs**: `instructor`'s default `Mode.TOOLS` failed against LM Studio (`Invalid tool_choice type:
'object'` — LM Studio's OpenAI-compatible surface only accepts string `tool_choice` values); `Mode.JSON`
failed too (`response_format.type` must be `json_schema`/`text`). **`Mode.MD_JSON`** — ask for a JSON
markdown block, parse that — worked cleanly, and is the more broadly compatible choice across
different local OpenAI-compatible servers for that reason. See `app/drafting/instructor_client.py`.

**`max_tokens` was underestimated twice, found live both times**: a reasoning-capable local model
(`qwen3.6-35b-a3b`) spends a variable, sometimes large number of tokens on its own thinking preamble
before the JSON block — 500 tokens truncated mid-output on simple inputs; 1500 still truncated on
real (longer, multi-paragraph) M3 issue descriptions specifically. Both looked like "structured output
failures" (triggering the degradation path) when the actual cause was running out of room to finish
thinking, not a schema problem. Current value (`_MAX_OUTPUT_TOKENS` in `service.py`) is 4000 — generous
on purpose, since `max_tokens` is a ceiling, not a target, so it doesn't slow down calls that finish
well under it.

### Structured-output stability (100 automated runs against real M3 descriptions)

`eval/task_draft_stability.py` — **10 real M3 issue descriptions (fetched live from jira-backend's
Postgres) x 10 repeats each = 100 calls**, not 100 arbitrary one-off prompts, because the actual
question is *stability*: given the same rough input, how consistent is the structured output across
repeated calls, not just "did it work once."

```bash
JIRA_BACKEND_PG_DSN=postgresql://poc:poc123@localhost:5432/pocdb \
AI_LLM_PROVIDER=openai_compatible AI_AGENT_MODEL=qwen/qwen3.6-35b-a3b AI_OPENAI_BASE_URL=http://localhost:1234/v1 \
python -m eval.task_draft_stability
```

**Measured result (local `qwen3.6-35b-a3b`, 100 calls, after the `max_tokens` fix above)**:

| metric | value |
|---|---|
| Schema-valid (non-degraded) | **100/100 (100.0%)** |
| Latency mean / P50 / P95 / max | 18.6s / 18.6s / 23.0s / 26.9s |

Per-description consistency (same input, 10 repeats each) is where the interesting signal is, not
just the headline pass rate:

- **`issue_type` is stable** — 10/10 identical for 8 of the 10 descriptions; the other two (5/5 and
  8/2 task-vs-story splits) are genuinely ambiguous inputs, not model flakiness — a human would
  plausibly disagree with themselves on those too.
- **`estimate_story_points`-or-`null` is *fully consistent per description*** — every description
  landed at either 10/10 estimated or 0/10 estimated, never a mix. The model isn't randomly guessing
  sometimes and correctly abstaining other times on the *same* input — its judgment of "is there
  enough signal" is stable, which is the property that actually matters (an estimate you can't trust
  to show up consistently for the same input is worse than no estimate field at all).
- **Labels have real but bounded variance** — 1 to 3 distinct label sets across a description's 10
  repeats, with one set usually dominant (6-10 of 10). Wording of the *title* varies run to run
  (expected, it's free text) while staying semantically consistent.

This run is what caught both `max_tokens` bugs above — 500 then 1500 both produced high degradation
rates against real (longer) M3 descriptions specifically, which shorter hand-written test prompts
never exposed. Found by running the eval, not by inspection — exactly what it exists to catch.

## LLM tracing + prompt versioning (Arize Phoenix, optional)

`GET /metrics` (below) gives aggregate latency/count metrics — good for "is this slow, and which
stage." It can't show you *one specific conversation's* full LLM call sequence. Phoenix's trace UI is
built for exactly that — open one trace, see the actual multi-turn exchange. Off by default
(`AI_PHOENIX_ENABLED=true` to turn on) — a real external service, not a required dependency.

```bash
docker run -d -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest
AI_PHOENIX_ENABLED=true uvicorn app.main:app --port 8200
# UI: http://localhost:6006
```

**Coverage is real but partial, stated precisely rather than implied** (`app/tracing.py` has the full
reasoning): OpenInference's instrumentors patch the underlying `anthropic`/`openai` SDK client
classes, so tracing only sees calls made *through those SDKs*.
- ✅ **Covered, verified live**: the AI-assisted task-drafting feature (`app/drafting/`, which uses
  `instructor.from_openai` wrapping a real `openai.AsyncOpenAI` client) — confirmed a real
  `ChatCompletion` LLM span landed in Phoenix for an actual `/draft-task` call against LM Studio.
- ⚠️ **Covered in principle, not verified live**: the production `AnthropicClient`
  (`app/llm/anthropic_client.py`) — instrumented the same way, but this project pins
  `anthropic==0.42.0` while the current `openinference-instrumentation-anthropic` wants `>=0.84.0`;
  Phoenix logs a `DependencyConflict` warning at startup and (per OpenInference's own behavior)
  likely skips instrumenting that SDK version rather than crashing. Not chased further since this
  path was already untested live in this project for an unrelated reason (no `ANTHROPIC_API_KEY`
  configured here).
- ❌ **Not covered**: the local-testing `OpenAICompatibleClient` (`app/llm/openai_compatible_client.py`)
  — that client talks to LM Studio over raw `httpx` on purpose (see its own docstring), not the
  `openai` SDK, so there's no SDK method for OpenInference to patch. This means the bulk of this
  project's actual tested traffic — every `/ask` call against LM Studio — is **not** visible in
  Phoenix today. The real fix would be routing that path through the real `openai` SDK too (it
  supports a custom `base_url`, so this is a plausible follow-up, not a structural blocker) or hand-
  writing spans in that client; not done here.

**Prompt versioning**: `scripts/push_prompt_to_phoenix.py` pushes the task-drafting Jinja2 template's
content to Phoenix as a tracked version (`template_format="NONE"` — Phoenix's native `MUSTACHE`/
`F_STRING` formats can't represent Jinja2's `{% if %}` control flow, so this stores the raw source for
version history/comparison in Phoenix's UI; **actual rendering still happens via `app/drafting/
prompts.py`'s Jinja2 `Environment`**, this is a tracking side-channel, not a runtime dependency). Run
it by hand after a deliberate prompt edit:

```bash
python -m scripts.push_prompt_to_phoenix
```

Verified live: ran it twice, got two distinct version IDs back, confirmed both exist server-side via
Phoenix's GraphQL API — real versioning, not a single write.

## Observability

Same pattern as `vectorization-service` (`app/observability.py` there) — `GET /metrics` exposes
request latency plus stage-specific histograms for *this* service's pipeline:
`agent_llm_call_seconds` (one `LLMClient.next_turn()` call), `agent_retrieval_call_seconds` (one
`search_knowledge_base` tool call), `agent_cache_lookup_seconds`, and `agent_retrieval_rounds` — a
distribution, not a latency, answering "is corrective retrieval actually firing in practice, and how
often" directly from a metric rather than grepping logs. `agent_cache_hits_total{hit_type="hit"|"miss"}`
tracks the cache's real hit rate over time. Every request also gets a correlation id (`x-request-id`
header + structured log field), same mechanism as `vectorization-service`.

## Evaluation — the classic Bay Area RAG/agent eval axes, measured

[`eval/scenarios.py`](eval/scenarios.py) defines 7 scenarios over the shared **AtlasCart** corpus
(the same one `vectorization-service/eval` uses for retrieval-only eval), across the four axes
agentic RAG systems are typically graded on:

- **grounded_answer** — baseline: retrieval + citation works.
- **abstention** (×2) — faithfulness / hallucination resistance. Asked about things genuinely absent
  from the corpus (a GDPR policy; the mobile app's language) — correct behavior is an honest refusal,
  never a plausible-sounding fabrication. Directly the requirement in this project's own
  `codex/RAG_EVAL_SPEC.md` ("say when the available data does not answer a question").
- **corrective_retrieval** (×2) — deliberately vague/casual phrasing ("the recommendations are
  broken", "promo math is wrong") that shares little vocabulary with the corpus text, testing whether
  the agent reformulates and searches again rather than giving up on one weak result.
- **time_aware** (×2) — distinguishing current from historical state, also explicit in
  `RAG_EVAL_SPEC.md`. The corpus bakes this in naturally: an issue's *description* reports the
  original bug, its *comment* reports the fix — answering "is X still broken?" correctly requires
  synthesizing both, not just the first hit.

[`eval/judge.py`](eval/judge.py) grades each run on **three independent axes** rather than one
pass/fail, deliberately: (1) `abstention_correct` — did it refuse exactly when it should; (2)
`retrieved_expected_issues` — did retrieval surface the right source, checked mechanically against
citations; (3) `grounded` — is the answer text actually supported by what was retrieved, checked by an
LLM judge (a real refusal is trivially grounded — it makes no claims to hallucinate). Splitting these
matters: a scenario can have **correct retrieval but an under-confident answer**, and collapsing that
into one boolean would hide exactly the distinction worth measuring.

```bash
# Prerequisite: ingest the corpus (from vectorization-service/) and have it running:
#   VEC_EMBEDDING_PROVIDER=openai VEC_OPENAI_BASE_URL=http://localhost:1234/v1 ... python -m eval.run_eval
#   uvicorn app.main:app --port 8100

AI_LLM_PROVIDER=openai_compatible AI_AGENT_MODEL=openai/gpt-oss-20b AI_OPENAI_BASE_URL=http://localhost:1234/v1 \
python -m eval.run_agentic_eval
```

### Measured result (local `openai/gpt-oss-20b`, a 20B model — not Claude)

| | score |
|---|---|
| Expected sources retrieved | **7/7** |
| Answers grounded (zero hallucinated claims, judge-checked) | **7/7** |
| Abstention behavior correct | 6/7 |
| **Fully correct (all three)** | **6/7** |

| category | result |
|---|---|
| grounded_answer | 1/1 |
| abstention | 2/2 |
| corrective_retrieval | 1/2 |
| time_aware | 2/2 |

**The one miss, read honestly, is the interesting result, not a failure to hide.** Scenario
`corrective-1` ("the recommendations are broken") retrieved the correct source (`ATLAS-8`, a
carousel surfacing out-of-stock items) after reformulating its query twice — corrective retrieval
*worked*. The judge still confirmed the response was grounded (an honest refusal makes no
unsupported claims). But the small local model was too conservative to make the inferential leap
from "broken" to "surfaces items customers can't buy," and abstained rather than answer. **Retrieval
succeeded; generation under-answered** — a real, well-known distinction between retrieval quality and
reasoning quality in RAG systems, and exactly what splitting the judge into three axes was designed
to surface rather than hide inside one pass/fail number. A stronger model (Claude Opus, the
production path) would plausibly close this gap — that's a live, re-runnable claim
(`AI_LLM_PROVIDER=anthropic`), not a guess.

## Tests

```bash
pip install -r requirements.txt
pytest   # CragAgent control-flow (re-retrieval, abstention, max-iteration cap, the space_ids
         # security boundary), pricing math, the semantic cache (exact/semantic hit, space_ids
         # isolation, TTL expiry, eviction), and the cache-failure-degrades-safely regression test —
         # all against fakes, no network.
```

## Concurrency — tested with a real load test, not assumed

[`../loadtest/`](../loadtest) runs [Locust](https://locust.io/) against a live instance of this
service. That exercise **found two real bugs** (an unhandled downstream failure leaking a raw 500
instead of a clean error, and a semantic-cache backend failure able to crash an otherwise-healthy
request) — both fixed, both regression-tested. It also surfaced a **real, honestly-documented
environment limitation**: a single local LM Studio process can't serve genuinely concurrent
chat-completion requests, which is a constraint of local single-model serving, not of this codebase,
and does not apply to the production `AI_LLM_PROVIDER=anthropic` path. Full writeup, numbers, and the
direct reproduction proving the failure is upstream: [`../loadtest/README.md`](../loadtest/README.md).

## Deployment

For running this outside your laptop, expose only the authenticated gateway (preferably behind HTTPS),
never this service's direct port. See [`../docs/AWS_DEPLOYMENT.md`](../docs/AWS_DEPLOYMENT.md).

## Known simplifications (portfolio scope)

Kept up to date as the system evolves — several items below were open gaps earlier in this project
and are now closed; see docs/RAG_ACCURACY_CASE_STUDIES.md for the full story behind each fix, not
just the current-state summary here.

**Still open:**
- **CORS is wide open (`allow_origins=["*"]`)** — a demo-only choice, made explicit rather than
  silently permissive (see `app/main.py`'s own comment). Space-level authorization is enforced now
  (see below), but a real deployment still needs a real, non-`*` CORS origin allowlist.
**Closed (kept here so the history of what was fixed doesn't get lost in git blame):**
- ~~No runtime faithfulness guardrail~~ — **Fixed.** RAGAS's faithfulness metric is an *offline*
  measurement; `crag_loop.py`'s `_check_faithfulness` (behind `AI_FAITHFULNESS_CHECK_ENABLED`, off by
  default on cost grounds) now checks a live answer's claims against its retrieved context before it
  reaches the caller, using the same verify-then-correct-once mechanism the existing post-generation
  verifiers use.
- ~~No naive-RAG baseline in the eval harness~~ — **Fixed.** `eval/naive_rag_baseline.py` scores plain
  top-k vector search + a single-shot answer on the identical question set, metrics and judge. The gap
  is large and specific (context_precision 0.913 vs. 0.175) — see Case Study 26.
- ~~Only single-call structured output; no multi-agent orchestration~~ — **Added, and measured before
  being trusted.** `app/planning/graph.py` runs `POST /plan-epic` through a LangGraph
  planner ↔ critic pipeline with a bounded, critique-driven revision loop, behind
  `AI_EPIC_PLANNING_MULTIAGENT_ENABLED` (off by default: multiple LLM calls instead of 1). The
  estimator node was removed after evaluation measured no benefit.
  `eval/planning_multiagent_eval.py` scores both arms against the same model on the same proposals,
  with the ship/no-ship decision rule written into the script *before* the first run. That module's own
  docstring states plainly that `crag_loop.py` already implements a bounded conditional loop with no
  graph library at all — the case for LangGraph here is declarative routing and free per-node events,
  not "the control flow requires it."
- ~~`space_ids` trusted unconditionally from the caller~~ — **Fixed.** The gateway already validates
  the JWT and forwards the real user's identity (`X-User-Id`/`X-Username`, see
  `PropagateUserHeadersGatewayFilter.java`); jira-backend already exposes a membership-scoped space
  list (`GET /api/spaces?userId=X`, the same endpoint the frontend's own `SpaceContext` calls). `/ask`,
  `/ask/stream`, `/search`, and workflow start/resume operations validate requested or persisted
  spaces via
  `app/auth/space_membership.py`, failing closed (502) if jira-backend is unreachable rather than
  silently permitting. Only enforced when `X-User-Id` is present — a direct caller with no gateway
  headers (this project's own eval/smoke-test scripts, all of which call `:8200` directly) is treated
  as a trusted internal caller and skipped, matching jira-backend's own
  `GatewayInternalAuthFilter.requiresUserIdentity()` per-route convention.
- ~~Durable workflow ids were bearer capabilities~~ — **Fixed.** Status, decision, retry,
  clarification, history, and time-travel routes now require the authenticated workflow owner and
  recheck that owner's membership in the persisted space. A user who guesses another thread UUID
  cannot read or operate it.
- ~~No reranking model after retrieval~~ — **Fixed.** A cross-encoder (`VEC_RERANK_ENABLED`, see
  vectorization-service) now reranks the fused hybrid candidate pool.
- ~~No streaming~~ — **Fixed.** `POST /ask/stream` (SSE) pushes agent-loop progress events as they
  happen; same underlying `CragAgent.ask()` call as the blocking `/ask`.
- ~~Judge and agent share one model~~ — **Fixed as a practice, not just a capability.** The harness
  always supported a different judge (`ragas_eval.py --llm-model`); it's now actually exercised that
  way in every ablation this project runs (e.g. `qwen2.5-72b-instruct` judging `gpt-5.6-luna`'s
  answers) rather than being a theoretical option nobody used.
- ~~CRAG grading is the agent's own reasoning, not a trained relevance classifier~~ — still true, but
  reclassified from "gap" to "deliberate design" once the corrective-retrieval-fallback pattern
  (`get_issue_comments`/`get_issue_details`/`get_issue_attachments`) made the actual failure mode this
  gap implied (bad self-assessment of retrieval quality) a non-issue in practice — see
  docs/RAG_ACCURACY_CASE_STUDIES.md's Case Study 25.
- ~~No prompt caching~~ — **Fixed for Anthropic.** `AnthropicClient` places cache breakpoints on the
  system/tools prefix and conversation tail, tracks cache-read/write tokens separately, and prices
  them with separate multipliers. The request time embedded in the system prompt is bucketed to five
  minutes so byte-identical prefixes can actually hit the cache; hermetic tests cover request shape
  and pricing. A live dollar-savings measurement still requires an Anthropic credential.
