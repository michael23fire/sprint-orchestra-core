# observability

Optional Prometheus + Grafana + Phoenix stack — the three pillars this project's own code exposes:
metrics (`GET /metrics` in both `vectorization-service` and `ai-service`, `app/observability.py` in
each), and LLM-call traces (`ai-service` only — see `ai-service/app/tracing.py`). Kept out of the main
`docker-compose.yml` deliberately, so a plain `docker compose up` doesn't start three extra containers
nobody asked for — bring this up explicitly when you want the dashboard/traces.

## Run it

```bash
# 1. Start vectorization-service and ai-service normally (see their own READMEs) — they must
#    already be running and reachable on :8100 / :8200 for Prometheus to scrape them.
#    For traces, ai-service also needs AI_PHOENIX_ENABLED=true (see ai-service/.env.example).

# 2. From the repo root:
docker compose -f observability/docker-compose.observability.yml up -d

# 3. Open Grafana (metrics) and Phoenix (traces)
open http://localhost:3001   # Grafana — anonymous admin access, local-demo only — see note below
open http://localhost:6006   # Phoenix — trace explorer
```

## Traces (Phoenix)

Metrics answer "is this slow, and which stage" — traces answer "show me the actual input/output for
*this one* `/ask` call, and every LLM call a multi-round CragAgent conversation made along the way."
`ai-service` sends OTLP spans to Phoenix's collector (`:4317`) whenever `AI_PHOENIX_ENABLED=true`;
browse them at `:6006`. Coverage is real but not universal — see `ai-service/app/tracing.py`'s
docstring for exactly which LLM client path (production Anthropic vs. local LM Studio testing) is
instrumented and why.

The dashboard **"Sprint Orchestra — RAG Platform"** is auto-provisioned (no manual "add data source" /
"import dashboard" clicking) — it should already be there under the "RAG Platform" folder the moment
Grafana starts, showing:

- Request rate + P95 latency, both services
- 5xx error rate
- `vectorization-service` `/search` stage breakdown (embed / retrieval / rerank latency)
- `ai-service` `/ask` stage breakdown (LLM call / retrieval tool call / cache lookup latency)
- Semantic cache hit rate
- Average corrective-retrieval rounds per question
- Cumulative estimated $ spent

Generate some traffic to see it move — hit `/ask` and `/search` a few times, or run
[`loadtest/`](../loadtest) against them.

## What's provisioned automatically vs. not

- `observability/grafana/provisioning/datasources/prometheus.yml` — the Prometheus data source, no
  manual setup.
- `observability/grafana/provisioning/dashboards/dashboards.yml` — tells Grafana to load any
  dashboard JSON in `grafana/dashboards/` on startup.
- `observability/prometheus.yml` — scrape config. Targets `host.docker.internal:8100` /
  `:8200`, since both services run directly on the host via `uvicorn`, not inside this compose
  network. Works out of the box on Docker Desktop (macOS/Windows); on Linux, add
  `extra_hosts: ["host.docker.internal:host-gateway"]` under the `prometheus` service in
  `docker-compose.observability.yml`.

## Security note

`GF_AUTH_ANONYMOUS_ENABLED=true` (admin role, no login) is set in
`docker-compose.observability.yml` purely for local-demo convenience — zero friction to open the
dashboard while showing this off. **Not appropriate for anything beyond a laptop demo.** A real
deployment would set a real `GF_SECURITY_ADMIN_PASSWORD` and turn anonymous access off.

## Stop it

```bash
docker compose -f observability/docker-compose.observability.yml down
```
