#!/usr/bin/env bash
# One-click local dev stack for AtlasCart / Ask-AI.
#
# Problem this solves: the full "Ask AI works in the browser" path is SEVEN things that all have to
# be up at once (frontend -> gateway -> jira-backend + ai-service -> vectorization-service -> DBs, plus
# LM Studio). After any one of them gets killed/restarted, the UI just says "server unavailable" with
# no clue which of the seven is the culprit. This script starts everything that isn't already running,
# in the right order, and prints one clear status table at the end.
#
# Safe to re-run any time: every step checks its port FIRST and skips anything already up, so this
# never double-starts a service or fights with one you started by hand.
#
# Usage:
#   scripts/dev_up.sh              start everything that isn't already running
#   scripts/dev_up.sh status       just print the status table, start nothing
#   scripts/dev_up.sh down         stop the processes THIS script started (PIDs in $PIDS_DIR)
#
# The dependency chain, so a failure is easy to place:
#   browser -> vite :5173 -> gateway :8080 -> jira-backend :8081   (issues/sprints CRUD, Postgres :5432)
#                                          \-> ai-service   :8200   (Ask-AI agent)
#                                                 -> vectorization-service :8100  (retrieval, vecdb :5433)
#                                                 -> LM Studio :1234              (local LLM + embeddings)
#                                                 -> Redis :6379                  (answer cache)
#
# Logs for anything THIS script starts land in $LOG_DIR (printed at the end) — `tail -f` one if a
# service comes up unhealthy. Anything already running when the script starts is left completely
# alone (not restarted, no log redirect), since that's usually a process you're actively working with.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STUDIO_DIR="$(cd "$ROOT/../sprint-orchestra-studio" 2>/dev/null && pwd || true)"
RUN_DIR="/tmp/sprint-orchestra-dev"
LOG_DIR="$RUN_DIR/logs"
PIDS_DIR="$RUN_DIR/pids"
mkdir -p "$LOG_DIR" "$PIDS_DIR"

# --- tiny output helpers ---------------------------------------------------------------------
c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_start=$'\033[36m'; c_off=$'\033[0m'
say_ok()    { printf "  ${c_ok}[ OK ]${c_off}    %s\n" "$1"; }
say_start() { printf "  ${c_start}[START]${c_off}    %s\n" "$1"; }
say_warn()  { printf "  ${c_warn}[WARN]${c_off}    %s\n" "$1"; }
say_skip()  { printf "  ${c_warn}[SKIP]${c_off}    %s\n" "$1"; }

port_open() { # port_open <port>
    lsof -tiTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# wait_for_port <port> <seconds> <label> — poll, don't sleep-and-hope.
wait_for_port() {
    local port="$1" timeout="$2" label="$3" waited=0
    while ! port_open "$port"; do
        sleep 1
        waited=$((waited + 1))
        if [ "$waited" -ge "$timeout" ]; then
            say_warn "$label still not listening on :$port after ${timeout}s — check its log"
            return 1
        fi
    done
    [ "$waited" -gt 0 ] && printf "           (took ~%ss)\n" "$waited"
    return 0
}

# --- action: status ---------------------------------------------------------------------------
print_status() {
    echo ""
    echo "=== Status ==="
    printf "  %-28s %-8s %s\n" "SERVICE" "PORT" "STATE"
    for row in \
        "Postgres (jira-backend DB):5432" \
        "pgvector DB (vecdb):5433" \
        "Redis (answer cache):6379" \
        "jira-backend:8081" \
        "gateway:8080" \
        "vectorization-service:8100" \
        "ai-service:8200" \
        "LM Studio:1234" \
        "frontend (vite):5173" \
    ; do
        name="${row%%:*}"; port="${row##*:}"
        if port_open "$port"; then state="${c_ok}up${c_off}"; else state="${c_warn}down${c_off}"; fi
        printf "  %-28s %-8s %b\n" "$name" "$port" "$state"
    done
    echo ""
}

if [ "${1:-}" = "status" ]; then
    print_status
    exit 0
fi

if [ "${1:-}" = "down" ]; then
    echo "Stopping processes this script started (recorded in $PIDS_DIR)..."
    for f in "$PIDS_DIR"/*.pid; do
        [ -e "$f" ] || continue
        pid="$(cat "$f")"
        name="$(basename "$f" .pid)"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" && say_ok "stopped $name (pid $pid)"
        else
            say_skip "$name (pid $pid) already gone"
        fi
        rm -f "$f"
    done
    exit 0
fi

echo "Starting the AtlasCart / Ask-AI local dev stack..."
echo "(safe to re-run — anything already up is left alone)"
echo ""

# --- 1. Docker infra: Postgres, vecdb, Redis, Kafka+ZK, MinIO, litellm -------------------------
echo "--- Docker infra ---"
if port_open 5432 && port_open 5433 && port_open 6379; then
    say_ok "Postgres / vecdb / Redis already up"
else
    say_start "docker compose up -d (postgres, vecdb, redis, kafka, minio, litellm...)"
    (cd "$ROOT" && docker compose up -d) >"$LOG_DIR/docker-compose.log" 2>&1
    wait_for_port 5432 60 "Postgres"
    wait_for_port 5433 60 "vecdb"
    wait_for_port 6379 30 "Redis"
fi

# --- 2. LM Studio: external GUI app, we can only check it, not launch its model for it --------
echo "--- LM Studio (local LLM + embeddings) ---"
if curl -sf -m 3 http://localhost:1234/v1/models >/dev/null 2>&1; then
    say_ok "reachable on :1234"
else
    say_warn "not reachable on :1234 — open the LM Studio app and start its local server "
    say_warn "(Developer tab -> Start Server), with qwen3.6-27b-mlx (or your model) loaded"
fi

# --- 3. jira-backend (Spring, :8081) ------------------------------------------------------------
echo "--- jira-backend :8081 ---"
if port_open 8081; then
    say_ok "already running"
else
    say_start "./gradlew :jira-backend:bootRun  (first run compiles — can take ~1-2 min)"
    (cd "$ROOT" && nohup ./gradlew :jira-backend:bootRun \
        > "$LOG_DIR/jira-backend.log" 2>&1 & echo $! > "$PIDS_DIR/jira-backend.pid")
    wait_for_port 8081 150 "jira-backend"
fi

# --- 4. gateway (Spring Cloud Gateway, :8080) — what the browser actually talks to -------------
echo "--- gateway :8080 ---"
if port_open 8080; then
    say_ok "already running"
else
    say_start "./gradlew :gateway:bootRun"
    # /api/ai/ask's default CircuitBreaker timeout (90s) was sized against the PRODUCTION path
    # (real Claude API). A local model through LM Studio is slower and single-threaded — CragAgent's
    # multi-round corrective retrieval routinely runs 60-90s+ per question here, and once the gateway
    # sees enough of those time out, its circuit breaker trips and short-circuits EVERY /ask call
    # (even fast ones) for the next 30s with "Service temporarily unavailable" — which reads as the
    # backend being down when it's actually healthy and just slow. Both knobs are already env-var
    # driven in application.yml; only the local dev launcher needs the more generous numbers.
    (cd "$ROOT" && \
        GATEWAY_AI_ASK_RESPONSE_TIMEOUT_MS="${GATEWAY_AI_ASK_RESPONSE_TIMEOUT_MS:-240000}" \
        GATEWAY_AI_ASK_CB_TIMEOUT="${GATEWAY_AI_ASK_CB_TIMEOUT:-240s}" \
        GATEWAY_AI_ASK_CB_MIN_CALLS="${GATEWAY_AI_ASK_CB_MIN_CALLS:-10}" \
        GATEWAY_AI_ASK_CB_OPEN_WAIT="${GATEWAY_AI_ASK_CB_OPEN_WAIT:-10s}" \
        nohup ./gradlew :gateway:bootRun \
        > "$LOG_DIR/gateway.log" 2>&1 & echo $! > "$PIDS_DIR/gateway.pid")
    wait_for_port 8080 150 "gateway"
fi

# --- 5. vectorization-service (Python, :8100) — retrieval over vecdb ---------------------------
echo "--- vectorization-service :8100 ---"
if port_open 8100; then
    say_ok "already running"
else
    say_start "uvicorn (vectorization-service venv)"
    (cd "$ROOT/vectorization-service" && source .venv/bin/activate && \
        VEC_EMBEDDING_PROVIDER=openai \
        VEC_OPENAI_BASE_URL=http://localhost:1234/v1 \
        VEC_EMBEDDING_MODEL=text-embedding-qwen3-0.6b-text-embedding \
        VEC_EMBEDDING_DIM=1024 \
        VEC_PG_DSN=postgresql://vec:vec123@localhost:5433/vecdb \
        VEC_KAFKA_BOOTSTRAP_SERVERS=localhost:9092 \
        nohup python -m uvicorn app.main:app --port 8100 \
        > "$LOG_DIR/vectorization-service.log" 2>&1 & echo $! > "$PIDS_DIR/vectorization-service.pid")
    wait_for_port 8100 60 "vectorization-service"
fi

# --- 6. ai-service (Python, :8200) — the Ask-AI agent, config from ai-service/.env -------------
echo "--- ai-service :8200 ---"
if port_open 8200; then
    say_ok "already running"
else
    say_start "uvicorn (ai-service venv, config from ai-service/.env)"
    (cd "$ROOT/ai-service" && source .venv/bin/activate && \
        nohup python -m uvicorn app.main:app --host 127.0.0.1 --port 8200 \
        > "$LOG_DIR/ai-service.log" 2>&1 & echo $! > "$PIDS_DIR/ai-service.pid")
    wait_for_port 8200 60 "ai-service"
fi

# --- 7. frontend (Vite dev server) --------------------------------------------------------------
echo "--- frontend (vite) ---"
if port_open 5173; then
    say_ok "already running on :5173"
elif [ -z "$STUDIO_DIR" ]; then
    say_warn "sprint-orchestra-studio not found next to this repo — start it manually (npm run dev)"
else
    say_start "npm run dev (sprint-orchestra-studio)"
    (cd "$STUDIO_DIR" && nohup npm run dev \
        > "$LOG_DIR/frontend.log" 2>&1 & echo $! > "$PIDS_DIR/frontend.pid")
    wait_for_port 5173 30 "frontend"
fi

print_status
echo "Logs for anything started just now: $LOG_DIR/"
echo "Open the app: http://localhost:5173  (log in, then click 'Ask AI')"
echo ""
echo "Reminder: the Ask-AI panel's 'Search in' defaults to ALL your spaces checked — for results"
echo "that match ai-service/eval/ask_ingestion_smoke.py, uncheck everything except AtlasCart first."
echo ""
echo "Run 'scripts/dev_up.sh status' any time to re-check without starting anything."
echo "Run 'scripts/dev_up.sh down' to stop only what this script itself started."
