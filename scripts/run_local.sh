#!/usr/bin/env bash
# Start the gud API and, when ui/ is present, the Vite dev server.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="${PYTHON:-python3}"
fi

PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "API  http://127.0.0.1:8787"
"$PY" -m gud.api &
PIDS+=($!)

if [[ -d "$ROOT/ui" ]]; then
  echo "UI   http://127.0.0.1:5173"
  (cd "$ROOT/ui" && npm run dev -- --host 127.0.0.1) &
  PIDS+=($!)
fi

wait
