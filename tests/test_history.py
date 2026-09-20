import json

from fastapi.testclient import TestClient

from transmux.app import create_app
from transmux.jobs import Worker
from transmux.store import Store
from test_workflows import FakeRunner, TinyEmbeddings, add_source
from transmux.rag import Rag


async def test_extraction_updates_both_files_and_snapshots(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codebuddy')['id']
    add_source(store, pid, 'corpus')
    original = store.snapshot_config(pid, 'style')
    task = store.enqueue(pid, 'style', {})
    await Worker(store, FakeRunner(), Rag(TinyEmbeddings())).execute(task)
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'succeeded'
    assert 'doctor' not in store.snapshot_config(pid, 'style')['content']
    assert json.loads(store.snapshot_config(pid, 'terms')['content'])['rows'][0]['term'] == 'doctor'
    assert json.loads(store.snapshot_config(pid, 'mappings')['content'])['rows'] == []
    history = store.rows('SELECT * FROM config_history WHERE name=? ORDER BY id', ('style',))
    assert history[0]['content'] == original['content']
    assert history[-1]['source'] == 'extraction'
    assert history[-1]['job'] == task['id']
    store.db.close()


async def test_style_conflict_prevents_both_updates(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codebuddy')['id']
    add_source(store, pid, 'corpus')

    class ConcurrentRunner(FakeRunner):
        async def run(self, *args):
            (store.workspace(pid) / 'style.md').write_text('用户新风格')
            return await super().run(*args)

    task = store.enqueue(pid, 'style', {})
    await Worker(store, ConcurrentRunner()).execute(task)
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'needs_attention'
    assert store.snapshot_config(pid, 'style')['content'] == '用户新风格'
    assert json.loads(store.snapshot_config(pid, 'terms')['content'])['rows'] == []
    store.db.close()


async def test_failed_agent_edits_are_snapshotted(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codebuddy')['id']

    class FailingRunner:
        async def run(self, *args):
            (store.workspace(pid) / 'glossary.md').write_text('失败前的修改')
            raise RuntimeError('Agent stopped')

    await Worker(store, FailingRunner()).execute(store.enqueue(pid, 'chat', {'message':'调整术语'}))
    history = store.rows("SELECT * FROM config_history WHERE name='glossary' ORDER BY id")
    assert history[-1]['content'] == '失败前的修改'
    assert history[-1]['source'] == 'agent'
    store.db.close()


def test_history_restore_isolation_conflict_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id':'codex','available':True}])
    with TestClient(create_app(tmp_path)) as client:
        pid = client.post('/api/projects', json={'name':'test','agent':'codex'}).json()['id']
        base = f'/api/projects/{pid}/config/style'
        initial = client.get(base).json()
        old = client.get(base+'/history').json()[0]
        latest = client.put(base, json={'content':'Use clear and concise English.','revision':initial['revision']}).json()
        client.put(base, json={'content':'Use clear and concise English.','revision':latest['revision']})
        assert len(client.get(base+'/history').json()) == 2
        url = base + f"/history/{old['id']}/restore"
        assert client.post(url,json={'revision':initial['revision']}).status_code == 409
        restored = client.post(url,json={'revision':latest['revision']})
        assert restored.json()['content'] == initial['content']
        history = client.get(base+'/history').json()
        assert history[0]['source'] == 'restore'
        assert history[1]['content'] == 'Use clear and concise English.'
        other = client.post('/api/projects', json={'name':'other','agent':'codex'}).json()['id']
        assert client.post(f'/api/projects/{other}/config/style/history/{old["id"]}/restore',json={'revision':initial['revision']}).status_code == 404
        assert client.get(base+f"/history?before={history[0]['id']}").json()[0]['id'] == history[1]['id']
    with TestClient(create_app(tmp_path)) as client:
        assert len(client.get(base+'/history').json()) == 3
        assert client.get(base).json()['content'] == initial['content']
