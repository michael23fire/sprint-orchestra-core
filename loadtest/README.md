# loadtest

Concurrency/load testing for both services, using [Locust](https://locust.io/) — chosen because it's
the tool most companies actually reach for (vs. hand-rolling an `asyncio.gather` script): Python test
scripts (not a DSL), a real distribution of requests over time via `wait_time`, and built-in
percentile reporting.

This is not a synthetic exercise — running it found **two real bugs**, fixed them, and surfaced one
**environment limitation that is not a code bug** and is worth understanding as clearly as the bugs
were. All three are documented below with the actual numbers, not summarized as "concurrency was
tested."

## Setup

```bash
pip install -r loadtest/requirements.txt
```

Needs both services running against real infra (see each service's own README for how to start
them) — these hit real HTTP endpoints, not mocks.

## 1. vectorization-service `/search` — the result: solid

```bash
locust -f loadtest/locustfile_vectorization.py --host http://localhost:8100 \
    --headless -u 50 -r 10 -t 30s --csv loadtest/results/vectorization
```

**Measured** (50 concurrent users, real hybrid/vector/lexical queries against the ingested AtlasCart
corpus, real embeddings via a local LM Studio model, reranking **on**):

| metric | value |
|---|---|
| Total requests | 1,702 |
| Failures | **0 (0.00%)** |
| Throughput | 62 req/s |
| P50 latency | 620 ms |
| P95 latency | 820 ms |
| P99 latency | 880 ms |

Re-run with reranking **off** for comparison: P50 610 ms, P95 770 ms — reranking (a local
cross-encoder, CPU/MPS-bound) added only ~10-50ms at this load, not the dominant cost. That was a
genuine question going in (does the reranker become the bottleneck under load?) — measured, not
assumed: it doesn't, at least not at this scale.

### P95 optimization attempt: the pool-size hypothesis was wrong, and testing it proved it

This README originally guessed `VEC_PG_POOL_MAX_SIZE` (default 8) was the next lever, reasoning that
50 concurrent requests sharing 8 pooled connections would queue for a connection. That guess was never
actually tested — it was speculation dressed as a conclusion. Testing it properly:

```bash
# same locust command as above, with VEC_PG_POOL_MAX_SIZE=32 (4x)
```

**Result: P95 got *worse*, not better** — 1400ms vs. 820ms at the default pool size, with a long tail
out to 6.6s. Bumping the pool size was the wrong fix. Checking the actual stage-latency breakdown this
project's own observability work built (`search_embed_seconds` vs. `search_retrieval_seconds`,
`vectorization-service`'s `/metrics`) settled it immediately:

| stage | P95 |
|---|---|
| embedding call (LM Studio) | **2.48s** |
| Postgres retrieval (pgvector query itself) | **5ms** |

The Postgres side — the thing the pool-size guess targeted — is essentially instant even under load.
The real bottleneck is the **embedding call to the local LM Studio server**, which cannot serve truly
concurrent requests well (the same root cause already documented above in the `ai-service` findings:
a single local model process serializes concurrent requests). Pool size was never in the critical
path. The correct fix isn't a config tweak in this codebase at all — it's what the architecture
already recommends as the default: a **hosted embedding provider** (Voyage/OpenAI, this project's
documented default — see `vectorization-service/README.md` "Embeddings") built to actually serve
concurrent traffic, instead of a single local desktop inference process. Reverted the pool-size change
since it demonstrably didn't help; kept the default.

The lesson worth stating plainly: the original guess was plausible-sounding and wrong. Actually
measuring the stage breakdown — not re-guessing harder — is what found the real answer.

Zero failures at 50 concurrent users is the actual "production grade" evidence for this endpoint —
not a claim, a number.

## 2. ai-service `/ask` — found 2 real bugs, fixed both, and hit a real environment limit

```bash
locust -f loadtest/locustfile_ai_service.py --host http://localhost:8200 \
    --headless -u 5 -r 1 -t 75s --csv loadtest/results/ai_service
```

This is the more interesting result, and it's reported honestly rather than cleaned up after the
fact.

### First run: 73% failure rate, raw unhandled 500s

The very first run of this test — before any of the fixes below — returned `HTTPError('500 Server
Error')` on 44 of 60 requests. That's exactly what a load test is *for*: this bug never showed up in
any of the earlier single-request manual testing or the 6 unit tests in `tests/test_crag_loop.py`,
because both only ever exercise one request at a time. Reading the actual traceback in the logs
found two distinct, real problems:

**Bug 1 — an unhandled downstream failure produced a raw 500 with a leaked traceback**, instead of a
clean error response. Root cause, confirmed by reproducing it directly with concurrent raw HTTP calls
against the local LLM server (LM Studio): a **single local model process can't serve two requests at
once** — a second request arriving while the first is still generating gets back a 400 or 500 from
LM Studio itself, not from this codebase. That's a real constraint of local single-instance model
serving, not a bug (see the "known limitation" section below) — but this codebase's handling of that
failure *was* a bug: it propagated as an unhandled exception all the way to FastAPI's generic 500
handler.

  Fix: `app/llm/openai_compatible_client.py` now retries transient 5xx responses with backoff
  (`tenacity`, matching the exact retry pattern `vectorization-service`'s embedder already uses for
  its own transient API failures — see that service's `app/ingest/embedder.py`), and
  `app/api/routes.py`'s `/ask` handler now catches whatever still fails after retries and returns a
  clean `502` with a `detail` message, logged server-side via `logger.exception` — the same
  "clean 502, don't leak internals" pattern `vectorization-service`'s own `/search` already used
  (this service just hadn't caught up to it yet). See `tests/test_crag_loop.py` — no, this one has no
  dedicated unit test because it requires two genuinely concurrent real HTTP calls to a live LLM
  server to observe: it can't be reproduced with `FakeLLM`. The load test *is* the test for this bug.

**Bug 2 — a semantic-cache backend failure could crash a request that had nothing to do with the
cache.** `POST /ask` calls `cache.get()` before doing any real work, and `cache.put()` after — under
load, `vectorization-service`'s `/embed` endpoint (which the cache calls to embed the question for
semantic matching, see `app/cache/embedding_client.py`) occasionally errored under the same
concurrent-LLM-server pressure, and that exception was unhandled, producing a second class of raw
500 — even for a request whose *actual* LLM call would have succeeded.

  Fix: `app/api/routes.py` now wraps both cache calls in `_safe_cache_get` / `_safe_cache_put`, which
  log a warning and degrade to "treat as a cache miss" / "skip the write" on any exception. This is a
  correctness principle, not just a load-test patch: **caching is an optimization, never a
  correctness dependency** — a broken cache backend must never be able to turn a request that would
  otherwise have succeeded into a failure. Regression-tested in
  `tests/test_routes_cache_safety.py` with a cache double that always raises.

### After both fixes: clean errors, but still real failures — and that's the honest result

```
POST /ask   20 reqs   20 failures (100%)   HTTPError('502 Bad Gateway')
```

Re-running after the fix still shows failures — but now they're **clean 502s with a logged reason**,
not raw 500 tracebacks, and reproducing the same concurrent calls directly against LM Studio (bypass
ing this codebase entirely) shows the identical failure pattern. That confirms the remaining failures
are not a bug in this codebase — they're a hard limit of the **local test environment**:

> **Known limitation, not a bug**: a single local LM Studio process serving one loaded model cannot
> handle genuinely concurrent chat-completion requests — even 2 concurrent users produced a 75%
> failure rate in a dedicated re-run. This is a property of running a 20B model on one desktop
> machine's inference server, not of `ai-service`'s code, and it would **not** apply to the production
> path (`AI_LLM_PROVIDER=anthropic`): Anthropic's API is built to serve many concurrent requests, so
> this specific failure mode is a local-dev-only ceiling. This is exactly the kind of thing a
> concurrency test is supposed to surface: not just "did my code crash" but "where is the actual
> bottleneck, and is it mine to fix."

What this load test *does* prove, with the two bugs fixed: this codebase's own concurrency handling
(cache safety, clean error responses, retry-then-give-up-cleanly) is correct — the remaining failures
are attributable, with direct reproduction evidence, to the upstream local model server, not to
`ai-service`.

## What this doesn't cover (known scope limits of this load test)

- Not run against the production Claude API path — Anthropic's API has its own (much higher, and
  documented) concurrency limits; that's a different, real number to go measure separately if this
  is ever load-tested against a live `ANTHROPIC_API_KEY`.
- No sustained soak test (minutes-to-hours) — only short bursts (30-90s), enough to surface
  concurrency bugs, not to characterize long-run memory/connection-leak behavior.
- Single-machine test — client (Locust) and both services ran on the same laptop, so network latency
  isn't represented; a real deployment (see `../docs/AWS_DEPLOYMENT.md`) would add real network RTT.
