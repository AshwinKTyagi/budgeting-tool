#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "Missing .venv — run: python3.12 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'" >&2
  exit 1
fi

if [[ ! -d web/node_modules ]]; then
  (cd web && npm install)
fi

cleanup() {
  kill "${UVICORN_PID:-}" "${VITE_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

.venv/bin/uvicorn serve:app --reload --host 127.0.0.1 --port 8000 &
UVICORN_PID=$!

(cd web && npm run dev -- --host 127.0.0.1 --port 5173) &
VITE_PID=$!

echo "API:  http://127.0.0.1:8000/docs"
echo "UI:   http://127.0.0.1:5173   (use this for frontend)"
wait