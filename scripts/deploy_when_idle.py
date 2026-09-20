"""Restart after running jobs finish; queued jobs remain persisted for the new scheduler."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time


def main():
    root = Path(__file__).resolve().parents[1]
    data = root / 'data'
    expected_pid = int((data / 'server.pid').read_text())

    def status(state, **details):
        (data / 'deployment.json').write_text(json.dumps({'state': state, 'time': time.time(), **details}))
        print(state, details, flush=True)

    status('waiting_for_idle', server_pid=expected_pid)
    while True:
        if int((data / 'server.pid').read_text()) != expected_pid:
            status('superseded')
            return
        with sqlite3.connect(data / 'transmux.sqlite3') as db:
            busy = db.execute("SELECT count(*) FROM jobs WHERE state='running'").fetchone()[0]
        if not busy:
            break
        time.sleep(5)
    cmdline = Path(f'/proc/{expected_pid}/cmdline').read_bytes()
    if b'from transmux.app import main' not in cmdline:
        raise RuntimeError('Unexpected server process; restart aborted')
    with sqlite3.connect(data / 'transmux.sqlite3') as db:
        backup = sqlite3.connect(data / f'before-language-upgrade-{int(time.time())}.sqlite3')
        db.backup(backup)
        backup.close()
        # Prevent new jobs from starting between the idle check and process shutdown.
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT count(*) FROM jobs WHERE state='running'").fetchone()[0]:
            raise RuntimeError('Queue became busy; rerun the deployment helper')
        status('restarting')
        os.kill(expected_pid, signal.SIGTERM)
        for _ in range(100):
            if not Path(f'/proc/{expected_pid}').exists():
                break
            time.sleep(.1)
        else:
            os.kill(expected_pid, signal.SIGKILL)
            time.sleep(.5)
    env = dict(os.environ, TRANSMUX_HOST='192.168.1.220',
               TRANSMUX_ALLOWED_HOSTS='192.168.1.220,localhost,127.0.0.1', PORT='8765', TRANSMUX_DATA=str(data))
    with (data / 'server.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-c', 'from transmux.app import main; main()'], cwd=root,
                                   env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    (data / 'server.pid').write_text(str(process.pid))
    import httpx
    with httpx.Client(trust_env=False, timeout=5) as client:
        for _ in range(30):
            try:
                response = client.get('http://192.168.1.220:8765/api/projects')
                response.raise_for_status()
                if all('target_language' in project for project in response.json()) and client.get('http://192.168.1.220:8765/api/health').json().get('workflow_version') == 15:
                    status('deployed', server_pid=process.pid)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
    raise RuntimeError('New service did not pass health verification; inspect data/server.log')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        path = Path(__file__).resolve().parents[1] / 'data' / 'deployment.json'
        path.write_text(json.dumps({'state':'failed', 'message':str(exc), 'time':time.time()}))
        raise
