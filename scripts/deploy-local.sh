#!/usr/bin/env bash
# Run on the host: bash scripts/deploy-local.sh
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
if ! command -v codex >/dev/null && ! command -v codebuddy >/dev/null; then
    echo 'Install and log in to Codex or CodeBuddy before deploying.' >&2
    exit 1
fi
systemctl --user show-environment >/dev/null
if ! systemctl --user is-active --quiet transmux-local.service; then
    "$PYTHON_BIN" - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 8765))
PY
fi
if command -v uv >/dev/null; then
    if [[ ! -x .venv/bin/python ]]; then
        uv venv --python "$PYTHON_BIN" .venv
    fi
    uv pip install --python .venv/bin/python -e .
else
    if [[ ! -x .venv/bin/python ]]; then
        "$PYTHON_BIN" -m venv .venv
    fi
    .venv/bin/python -m ensurepip --upgrade
    .venv/bin/python -m pip install -e .
fi

# Generate a distinct service so the historical LAN service is not overwritten.
.venv/bin/python - <<'PY'
import os
from pathlib import Path

root = Path.cwd()
runtime_path = ':'.join(dict.fromkeys(
    str(Path(entry).resolve()) for entry in os.environ['PATH'].split(':') if entry
))
def quoted(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'

unit = Path.home() / '.config/systemd/user/transmux-local.service'
unit.parent.mkdir(parents=True, exist_ok=True)
unit.write_text('\n'.join([
    '[Unit]', 'Description=TransMux v2 local workspace', 'After=network-online.target', '',
    '[Service]', 'Type=simple', f'WorkingDirectory={str(root).replace("%", "%%")}',
    f'Environment={quoted("PATH=" + runtime_path)}',
    f'ExecStart=/bin/bash {quoted(root / "scripts/run-local.sh")}',
    'UMask=0077', 'Restart=on-failure', 'RestartSec=5', 'TimeoutStopSec=150', '',
    '[Install]', 'WantedBy=default.target', '',
]))
print(f'Service written: {unit}')
PY
systemctl --user daemon-reload
systemctl --user enable transmux-local.service
systemctl --user restart transmux-local.service
sleep 2
systemctl --user is-active --quiet transmux-local.service
.venv/bin/python - <<'PY'
import json
import shlex
import subprocess
import time
import urllib.request

settings = dict(item.split('=', 1) for item in shlex.split(subprocess.check_output(
    ['systemctl', '--user', 'show', 'transmux-local.service', '-p', 'Environment', '--value'],
    text=True,
)) if '=' in item)
host = settings.get('TRANSMUX_HOST', '127.0.0.1')
port = settings.get('PORT', '8765')
base_url = f'http://{host}:{port}'
for attempt in range(30):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base_url + '/api/health', timeout=2) as response:
            health = json.load(response)
        assert health['status'] == 'ok' and health['workflow_version'] == 20, health
        print(f'TransMux v2 ready: {base_url}')
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(1)
PY
systemctl --user is-active --quiet transmux-local.service
