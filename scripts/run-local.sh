#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
export TRANSMUX_V2_DATA="${TRANSMUX_V2_DATA:-$PROJECT_ROOT/data-v2}"
export TRANSMUX_HOST="${TRANSMUX_HOST:-127.0.0.1}"
export TRANSMUX_ALLOWED_HOSTS="${TRANSMUX_ALLOWED_HOSTS:-localhost,127.0.0.1,[::1]}"
export PORT="${PORT:-8765}"
umask 0077
exec "$PROJECT_ROOT/.venv/bin/python" -m uvicorn transmux.v2:app \
    --host "$TRANSMUX_HOST" --port "$PORT" --workers 1
