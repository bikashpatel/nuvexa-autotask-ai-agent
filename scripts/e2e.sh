#!/usr/bin/env bash
# System end-to-end tests: start an isolated Docker Compose stack, run e2e/ against it, tear it down.
#
#   scripts/e2e.sh                 # build + run everything (API, resilience, browser)
#   scripts/e2e.sh -k ui           # extra args are passed to pytest
#   E2E_KEEP=1 scripts/e2e.sh      # keep the stack running afterwards for debugging
#
# Uses its own Compose project and host ports so it never touches a developer's running stack.
set -euo pipefail

cd "$(dirname "$0")/.."
export E2E_COMPOSE_PROJECT="${E2E_COMPOSE_PROJECT:-nuvexa-e2e}"
export NUVEXA_WEB_PORT="${NUVEXA_WEB_PORT:-28080}"
export NUVEXA_API_PORT="${NUVEXA_API_PORT:-28000}"
export NUVEXA_DB_PORT="${NUVEXA_DB_PORT:-25432}"
export E2E_BASE_URL="${E2E_BASE_URL:-http://127.0.0.1:${NUVEXA_WEB_PORT}}"

compose() { docker compose -p "$E2E_COMPOSE_PROJECT" -f docker-compose.yml "$@"; }

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    mkdir -p e2e-artifacts
    compose logs --no-color > e2e-artifacts/compose.log 2>&1 || true
    echo "e2e failed — container logs in e2e-artifacts/compose.log" >&2
  fi
  if [ "${E2E_KEEP:-0}" != "1" ]; then
    compose down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

compose down -v --remove-orphans >/dev/null 2>&1 || true
compose up -d --build

echo "Waiting for ${E2E_BASE_URL}/api/v1/health/ready ..."
for _ in $(seq 1 90); do
  if curl -fsS "${E2E_BASE_URL}/api/v1/health/ready" >/dev/null 2>&1; then break; fi
  sleep 2
done

uv sync --locked --group e2e --quiet
uv run playwright install chromium >/dev/null
uv run pytest e2e -p no:cacheprovider -o addopts="" -ra "$@"
