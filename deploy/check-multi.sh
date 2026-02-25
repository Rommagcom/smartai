#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPECTED_WORKERS="${1:-1}"

if ! [[ "$EXPECTED_WORKERS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: expected worker count must be a non-negative integer"
  exit 1
fi

cd "$ROOT_DIR"

COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.multi.yml)

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed"
  exit 1
fi

services_output="$(docker compose "${COMPOSE_FILES[@]}" --profile multi ps --status running --services 2>/dev/null || true)"
if [[ -z "$services_output" ]]; then
  echo "ERROR: no running services found for multi profile"
  exit 1
fi

count_service() {
  local name="$1"
  echo "$services_output" | grep -E "^${name}$" | wc -l | tr -d ' '
}

scheduler_count="$(count_service scheduler-leader)"
worker_count="$(count_service worker)"
api_count="$(count_service api)"

echo "Running services summary:"
echo "  api: ${api_count}"
echo "  scheduler-leader: ${scheduler_count}"
echo "  worker: ${worker_count}"

if [[ "$scheduler_count" -ne 1 ]]; then
  echo "ERROR: scheduler-leader must have exactly 1 running instance (actual: ${scheduler_count})"
  exit 2
fi

if [[ "$worker_count" -ne "$EXPECTED_WORKERS" ]]; then
  echo "ERROR: worker count mismatch (expected: ${EXPECTED_WORKERS}, actual: ${worker_count})"
  exit 3
fi

if [[ "$api_count" -lt 1 ]]; then
  echo "ERROR: at least one api instance must be running"
  exit 4
fi

echo "OK: multi-instance topology is valid"
