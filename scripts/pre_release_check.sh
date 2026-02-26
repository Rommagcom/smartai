#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKERS="${WORKERS:-3}"
BASE_URL="${BASE_URL:-http://localhost:8000/api/v1}"
RUN_K6="${RUN_K6:-0}"
K6_MODE="${K6_MODE:-native}" # native|docker
SMOKE_MODE="${SMOKE_MODE:-container}" # container|host
PYTHON_BIN="${PYTHON_BIN:-}"

log() {
  echo "[pre-release] $*"
}

fail() {
  echo "[pre-release][ERROR] $*" >&2
  exit 1
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || fail "Required command not found: $cmd"
}

resolve_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    echo "$PYTHON_BIN"
    return
  fi

  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    echo "$ROOT_DIR/.venv/bin/python"
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
    return
  fi

  if command -v python >/dev/null 2>&1; then
    echo "python"
    return
  fi

  fail "Python executable not found. Set PYTHON_BIN or install python3."
}

run_k6_script() {
  local script="$1"
  if [[ "$K6_MODE" == "docker" ]]; then
    require_cmd docker
    docker run --rm -i --network host -v "$ROOT_DIR:/work" -w /work grafana/k6 run -e BASE_URL="$BASE_URL" "$script"
    return
  fi

  require_cmd k6
  k6 run -e BASE_URL="$BASE_URL" "$script"
}

cd "$ROOT_DIR"

log "Checking toolchain"
require_cmd docker

if ! docker info >/dev/null 2>&1; then
  fail "Docker daemon is not available"
fi

PYTHON_CMD=""
if [[ "$SMOKE_MODE" == "host" ]]; then
  PYTHON_CMD="$(resolve_python)"
  log "Python resolved: $PYTHON_CMD"
else
  log "Smoke mode: container"
fi

log "Starting multi-instance stack (workers=$WORKERS)"
docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker="$WORKERS"

log "Validating topology"
bash deploy/check-multi.sh "$WORKERS"

log "Running smoke_all"
if [[ "$SMOKE_MODE" == "container" ]]; then
  docker compose exec -T api python -m scripts.smoke_all
else
  "$PYTHON_CMD" -m scripts.smoke_all
fi

if [[ "$RUN_K6" == "1" ]]; then
  log "Running k6 chat burst"
  run_k6_script "scripts/load/k6_chat_worker_burst.js"

  log "Running k6 telegram polling soak"
  run_k6_script "scripts/load/k6_telegram_polling_soak.js"
else
  log "Skipping k6 (RUN_K6=$RUN_K6)"
fi

log "Pre-release checks passed"
