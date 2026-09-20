import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from transmux.agents import AgentRunner, command
from transmux.app import create_app
from transmux.jobs import Worker
from transmux.store import Store


def test_existing_database_migration(tmp_path):
    db = sqlite3.connect(tmp_path / 'transmux.sqlite3')
    db.execute('CREATE TABLE projects (id TEXT PRIMARY KEY,name TEXT,agent TEXT,session TEXT,created REAL)')
    db.execute("INSERT INTO projects VALUES ('old','Old','codex','session',1)")
    db.commit()
    db.close()
    store = Store(tmp_path)
    assert store.project('old')['model'] is None
    assert store.project('old')['session'] == 'session'
    assert store.project('old')['target_language'] == 'en'
    assert store.project('old')['use_rag'] == 1
    store.db.close()


@pytest.mark.parametrize('agent', ['codex', 'codebuddy'])
@pytest.mark.parametrize('session', [None, 'existing-session'])
def test_model_cli_argument(agent, session):
    args = command(agent, session, model='custom/model-1')
    assert args[args.index('--model') + 1] == 'custom/model-1'
    assert '--model' not in command(agent, session)


def test_project_models_api(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id':'codex','available':True}, {'id':'codebuddy','available':True}])
    monkeypatch.setattr('transmux.app.model_choices', lambda agent: [agent + '-model'])
    with TestClient(create_app(tmp_path)) as client:
        for agent in ('codex', 'codebuddy'):
            project = client.post('/api/projects', json={'name':agent,'agent':agent,'model':'initial'}).json()
            url = '/api/projects/' + project['id']
            assert project['model'] == 'initial'
            assert client.get(f'/api/agents/{agent}/models').json()['models'] == [agent + '-model']
            updated = client.patch(url, json={'model':'next'}).json()
            assert updated['model'] == 'next' and updated['agent'] == agent
            assert client.get(url).json()['model'] == 'next'
            assert client.patch(url, json={'model':'  '}).json()['model'] is None
            assert client.patch(url, json={'agent':'codex','model':'bad'}).status_code == 422
            assert client.patch(url, json={'model':'--invalid'}).status_code == 422


async def test_running_job_keeps_model_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path)
    pid = store.create_project('Test', 'codex', 'old-model')['id']
    queued = store.enqueue(pid, 'chat', {'message':'test'})
    # An already queued task uses the model selected when execution begins.
    store.execute('UPDATE projects SET model=? WHERE id=?', ('start-model', pid))
    worker = Worker(store)

    async def perform(job):
        assert json.loads(job['payload'])['_model'] == 'start-model'
        store.execute('UPDATE projects SET model=? WHERE id=?', ('future-model', pid))
        return 'done'

    monkeypatch.setattr(worker, 'perform', perform)
    await worker.execute(queued)
    run = store.workspace(pid) / 'runs' / queued['id']
    run.mkdir(parents=True)
    seen = []

    def inspect_command(agent, session, schema, model):
        seen.append(model)
        raise RuntimeError('stop before subprocess')

    monkeypatch.setattr('transmux.agents.command', inspect_command)
    with pytest.raises(RuntimeError, match='stop before subprocess'):
        await AgentRunner(store).run(pid, queued['id'], 'next review pass')
    assert seen == ['start-model']
    assert store.project(pid)['model'] == 'future-model'
    store.db.close()
