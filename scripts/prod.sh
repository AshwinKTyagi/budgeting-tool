#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "Missing .venv — run: python3.12 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'" >&2
  exit 1
fi

(cd web && npm install && npm run build)

echo "UI+API: http://127.0.0.1:8000"
exec .venv/bin/uvicorn serve:app --host 127.0.0.1 --port 8000