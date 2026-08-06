#!/usr/bin/env bash
# Local equivalent of .github/workflows/ci.yml — run all four services' test suites in one shot,
# without waiting on/pushing to GitHub. Same jobs, same order, meant to be run by hand after any
# change (or wired into a pre-commit/pre-push git hook — see the bottom of this file's header).
#
# Why this exists alongside real CI, not instead of it: CI still matters even for a local-only
# project (it's the guardrail that runs whether or not you remember to; see .github/workflows/ci.yml's
# own comment) — but this project isn't deployed anywhere yet, so there's no push-triggered pipeline
# to lean on day-to-day. This script is the same three test suites, runnable on demand, in ~30s.
#
# Usage:
#   scripts/run_all_tests.sh              run everything
#   scripts/run_all_tests.sh ai           just ai-service
#   scripts/run_all_tests.sh vec          just vectorization-service
#   scripts/run_all_tests.sh backend      just jira-backend (needs Postgres up — see scripts/dev_up.sh)
#   scripts/run_all_tests.sh gateway      just gateway
#
# To run this automatically before every commit:
#   ln -s ../../scripts/run_all_tests.sh .git/hooks/pre-commit
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${1:-all}"
failed=0

c_ok=$'\033[32m'; c_fail=$'\033[31m'; c_off=$'\033[0m'

run_ai_service() {
    echo "--- ai-service ---"
    (cd "$ROOT/ai-service" && source .venv/bin/activate && python -m pytest -q) \
        && echo "${c_ok}[ OK ]${c_off} ai-service" \
        || { echo "${c_fail}[FAIL]${c_off} ai-service"; failed=1; }
}

run_vectorization_service() {
    echo "--- vectorization-service ---"
    (cd "$ROOT/vectorization-service" && source .venv/bin/activate && python -m pytest -q) \
        && echo "${c_ok}[ OK ]${c_off} vectorization-service" \
        || { echo "${c_fail}[FAIL]${c_off} vectorization-service"; failed=1; }
}

run_jira_backend() {
    echo "--- jira-backend ---"
    (cd "$ROOT" && ./gradlew :jira-backend:test --no-daemon -q) \
        && echo "${c_ok}[ OK ]${c_off} jira-backend" \
        || { echo "${c_fail}[FAIL]${c_off} jira-backend"; failed=1; }
}

run_gateway() {
    echo "--- gateway ---"
    (cd "$ROOT" && ./gradlew :gateway:test --no-daemon -q) \
        && echo "${c_ok}[ OK ]${c_off} gateway" \
        || { echo "${c_fail}[FAIL]${c_off} gateway"; failed=1; }
}

case "$target" in
    ai) run_ai_service ;;
    vec) run_vectorization_service ;;
    backend) run_jira_backend ;;
    gateway) run_gateway ;;
    all)
        run_ai_service
        run_vectorization_service
        run_jira_backend
        run_gateway
        ;;
    *)
        echo "Usage: $0 [all|ai|vec|backend|gateway]" >&2
        exit 2
        ;;
esac

exit "$failed"
