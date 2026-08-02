# litellm-gateway

An [LiteLLM Proxy](https://docs.litellm.ai/docs/simple_proxy) instance — one OpenAI-compatible
endpoint in front of every LLM backend this project talks to, adding centralized rate limiting and
per-key spend budgets that no individual service's own code implements.

## Why this exists, separate from `ai-service/app/llm/`

`ai-service` already has its own provider abstraction (`LLMClient` protocol, `AnthropicClient` /
`OpenAICompatibleClient`) — that solves *"how does this one process call whichever provider it's
configured for."* A gateway solves a different, org-level problem: rate limiting and spend budgets
**per caller** (not per service), model routing that can change without redeploying anything, and one
place to see total spend across every service that talks to it — the actual responsibility an AI
Platform team owns, distinct from what any single service's client code does. Not a replacement for
the existing abstraction; a layer in front of it.

## Run it

Optional infrastructure — not started by a plain `docker compose up` (needs the `litellm` profile):

```bash
docker exec poc-postgres psql -U poc -d pocdb -c "CREATE DATABASE litellm;"  # one-time; already
                                                                              # a database inside the
                                                                              # existing postgres
                                                                              # container, not a new one
docker compose --profile litellm up -d litellm
curl http://localhost:4000/health/liveliness   # "I'm alive!"
```

`litellm-gateway/config.yaml` routes six model names:

| `model_name` | backend | notes |
|---|---|---|
| `local-chat` | LM Studio (`qwen/qwen3.6-35b-a3b`) | this project's actual local dev model throughout — the requested "local vLLM" role, same OpenAI-compatible routing pattern either way |
| `local-embedding` | LM Studio (`text-embedding-qwen3-embedding-0.6b`) | |
| `vllm-local` | a real, running vLLM server (`vllm-server/`) | CPU-only, tiny model (Qwen2.5-0.5B) on this hardware — proves the deployment/routing config, not a throughput demo |
| `claude-production` | Anthropic `claude-opus-4-8` | reads `ANTHROPIC_API_KEY` — not configured in this environment, so real and correctly configured but not called live |
| `openai-production` | real OpenAI cloud (`gpt-5`) | reads `OPENAI_API_KEY` — same untested-live caveat; distinct from `local-chat`, which is LM Studio imitating the OpenAI wire format, not OpenAI itself |
| `azure-production` | Azure OpenAI (`azure/<deployment_name>`) | reads `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` — a genuinely different backend from OpenAI proper, not the same route with a different URL: LiteLLM's `azure/...` model prefix and Azure's own required `api_base`/`api_version` params. Same untested-live caveat. |

## Verified live — routing, tool-calling, rate limits, spend budgets

Every claim below was run against the actual container, not assumed from LiteLLM's docs:

**1. Chat routing to the local backend works:**
```bash
curl -X POST http://localhost:4000/chat/completions \
  -H "Authorization: Bearer sk-litellm-local-master-key" -H "Content-Type: application/json" \
  -d '{"model":"local-chat","max_tokens":30,"messages":[{"role":"user","content":"say hello"}]}'
# -> real completion, system_fingerprint confirms it's genuinely qwen/qwen3.6-35b-a3b underneath
```

**2. Tool-calling passes through intact** (critical — this is what `ai-service`'s CragAgent actually
needs, not just plain chat):
```bash
# tools=[...] in the request -> real tool_calls back: [{'function': {'name': 'get_weather', ...}}]
```

**3. Rate limiting — a virtual key with `rpm_limit: 2`, fired 3 requests back to back:**

| request | result |
|---|---|
| 1 | 200 OK |
| 2 | 200 OK |
| 3 | **429**, `"Rate limit exceeded ... Limit type: requests. Current limit: 2, Remaining: 0"` |

**4. Spend budget cap — a virtual key with `max_budget: $0.001`.** Local inference genuinely costs
$0 (same convention as `ai-service/app/llm/pricing.py`), so `config.yaml` assigns the `local-chat`
route a small **notional** per-token price (documented in the config file itself) purely so budget
enforcement has something non-zero to test against locally, with no real paid API key available in
this environment to test against instead:

| request | result |
|---|---|
| 1 | 200 OK |
| 2 | 200 OK |
| 3 | **429**, `"Budget has been exceeded! ... Current cost: 0.00159, Max budget: 0.001"` |
| 4 | 429 (stays blocked) |

**5. `ai-service` routes through the gateway with zero code changes** — its existing
`OpenAICompatibleClient` already speaks plain OpenAI-compatible HTTP, so pointing it at the gateway
instead of directly at LM Studio is a **config-only** change:

```bash
AI_LLM_PROVIDER=openai_compatible AI_AGENT_MODEL=local-chat \
AI_OPENAI_BASE_URL=http://localhost:4000 AI_OPENAI_API_KEY=<a litellm virtual key> \
uvicorn app.main:app --port 8200
```

Ran a real `/ask` question through this config — full corrective-RAG loop (retrieval, tool use,
grounded answer) worked end to end, routed through the gateway the whole way. This wasn't the
original reason the two-provider abstraction was built (that was for local-dev-without-a-cloud-key
testing), but it turned out to compose cleanly with a gateway sitting in front of it — worth noting
as a real, unplanned payoff of keeping that abstraction protocol-shaped rather than provider-specific.

## Admin API — creating a rate-limited, budget-capped key

```bash
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer sk-litellm-local-master-key" -H "Content-Type: application/json" \
  -d '{
    "key_alias": "ai-service-prod-key",
    "models": ["local-chat", "local-embedding", "claude-production"],
    "max_budget": 5.00,
    "rpm_limit": 60,
    "duration": "30d"
  }'
```

This is the realistic pattern for a real deployment: issue one virtual key per caller/team/service,
each with its own budget and rate limit, instead of every service sharing one raw provider API key
with no per-caller governance at all — the actual gap `ai-service`'s own cost tracking
(`app/stats.py`) left open: it *measures* spend, it never *stops* it.

## Known simplifications

- **Postgres reused, not a dedicated instance** — LiteLLM's virtual-key/budget/spend state needs a
  Postgres backend; this uses a new `litellm` *database* inside the existing `postgres` container
  (the one `jira-backend` already runs) rather than standing up a fourth Postgres container. Logically
  separate (different database, not shared tables), operationally coupled to that container's
  lifecycle — a reasonable trade for a portfolio-scope deployment, not what a real multi-tenant
  gateway would do.
- **Master key is a plaintext default in `docker-compose.yml`** (`sk-litellm-local-master-key`,
  overridable via `LITELLM_MASTER_KEY` env var) — fine for local dev, not how a real secret would be
  handled (see `docs/AWS_DEPLOYMENT.md` for the general "this is a demo, not hardened for a public
  deployment" posture already documented elsewhere in this project).
- **`claude-production` route untested live** — no `ANTHROPIC_API_KEY` in this environment, same
  caveat as the rest of this project's Anthropic path.
