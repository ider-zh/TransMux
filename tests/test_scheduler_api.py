import asyncio
import threading
import time

from fastapi.testclient import TestClient

from transmux.app import create_app
from transmux.jobs import Worker
from transmux.rag import Rag
from test_workflows import TinyEmbeddings


def test_cancel_endpoint_isolates_concurrent_projects(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id':a,'available':True} for a in ('codex','codebuddy')])
    release = threading.Event()

    class ControlledWorker(Worker):
        async def perform(self, job):
            while not release.is_set():
                await asyncio.sleep(.01)
            return 'done'

    app = create_app(tmp_path, lambda store: ControlledWorker(store, rag=Rag(TinyEmbeddings())))
    with TestClient(app) as client:
        bases = ['/api/projects/'+client.post('/api/projects', json={'name':agent,'agent':agent}).json()['id']
                 for agent in ('codex', 'codebuddy')]
        tasks = [client.post(base+'/jobs', json={'kind':'chat','message':'test'}).json()['id'] for base in bases]

        def wait_state(base, jid, expected):
            for _ in range(300):
                job = next(row for row in client.get(base+'/jobs').json() if row['id'] == jid)
                if job['state'] == expected:
                    return
                time.sleep(.01)
            raise AssertionError(f'Expected {expected}, got {job["state"]}')

        try:
            for base, jid in zip(bases, tasks):
                wait_state(base, jid, 'running')
            queued = client.post(bases[0]+'/jobs', json={'kind':'chat','message':'queued'}).json()['id']
            wait_state(bases[0], queued, 'queued')
            assert client.post(bases[1]+f'/jobs/{tasks[0]}/cancel').status_code == 404
            assert client.post(bases[0]+f'/jobs/{tasks[0]}/cancel').status_code == 200
            wait_state(bases[0], tasks[0], 'cancelled')
            wait_state(bases[0], queued, 'running')
            wait_state(bases[1], tasks[1], 'running')
            release.set()
            wait_state(bases[0], queued, 'succeeded')
            wait_state(bases[1], tasks[1], 'succeeded')
        finally:
            release.set()
