import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from transmux.app import create_app
from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import ConfigConflict, Store
from test_workflows import FakeRunner, TinyEmbeddings, add_source


def test_workspace_delete_cleans_only_selected_project_and_requires_confirmation(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('删除空间', 'codex')['id']
    other = store.create_project('保留空间', 'codex')['id']
    fid = add_source(store, pid)
    store.add_file(pid, 'result.docx', 'output', store.safe_path(pid, store.file(pid, fid)['path']))
    task = store.enqueue(pid, 'chat', {'message':'test'})
    with pytest.raises(ValueError, match='名称'):
        store.delete_project(pid, 'wrong')
    with pytest.raises(ConfigConflict):
        store.delete_project(pid, '删除空间')
    store.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (task['id'],))
    work = store.workspace(pid)
    store.delete_project(pid, '删除空间')
    assert not work.exists()
    assert not (tmp_path / 'published' / pid).exists()
    for table in ('jobs', 'events', 'config_history', 'files'):
        assert not store.rows(f'SELECT * FROM {table} WHERE project=?', (pid,))
    assert store.project(other)['name'] == '保留空间'
    assert store.snapshot_config(other, 'style')['content']
    store.db.close()


def test_corpus_delete_invalidates_rag_and_preserves_other_files_and_configs(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex')['id']
    fid = add_source(store, pid, 'corpus')
    work = store.workspace(pid)
    path = store.safe_path(pid, store.file(pid, fid)['path'])
    source = add_source(store, pid)
    rag = Rag(TinyEmbeddings())
    worker = Worker(store, FakeRunner(), rag)
    rag.build(work, worker.corpus(pid), 'en')
    (work / 'rag' / 'status.json').write_text('{"state":"ready"}')
    before = {name: store.snapshot_config(pid, name) for name in ('style','terms','mappings')}
    store.delete_corpus(pid, fid)
    assert not path.exists() and not path.with_name(path.name + '.json').exists()
    assert not (work / 'rag' / 'index.json').exists()
    assert not (work / 'rag' / 'status.json').exists()
    assert store.file(pid, source)
    assert before == {name: store.snapshot_config(pid, name) for name in before}
    rag.build(work, worker.corpus(pid), 'en')
    assert rag.search_many(work, worker.corpus(pid), ['doctor'], target='en') == [[]]
    with pytest.raises(ValueError, match='参考语料'):
        store.delete_corpus(pid, source)
    store.db.close()


def test_failed_database_delete_restores_files(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex')['id']
    fid = add_source(store, pid, 'corpus')
    path = store.safe_path(pid, store.file(pid, fid)['path'])
    store.db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON files BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.delete_project(pid, 'test')
    assert path.is_file()
    assert store.file(pid, fid)
    assert store.snapshot_config(pid, 'style')['content']
    store.db.close()


def test_delete_does_not_follow_workspace_symlinks(tmp_path):
    store = Store(tmp_path / 'data')
    pid = store.create_project('test', 'codex')['id']
    fid = add_source(store, pid, 'corpus')
    work = store.workspace(pid)
    original = work / store.file(pid, fid)['path']
    original.unlink()
    original.symlink_to(work / 'style.md')
    store.delete_corpus(pid, fid)
    assert (work / 'style.md').is_file()
    fid = add_source(store, pid, 'corpus')
    external = tmp_path / 'outside'
    external.mkdir()
    (external / 'index.json').write_text('keep')
    (work / 'rag').rmdir()
    (work / 'rag').symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match='外部路径'):
        store.delete_corpus(pid, fid)
    assert (external / 'index.json').read_text() == 'keep'
    assert store.file(pid, fid)
    store.db.close()


def test_delete_api_isolation_confirmation_and_upload_guard(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id':'codex','available':True}])
    app = create_app(tmp_path, lambda store: Worker(store, FakeRunner(), Rag(TinyEmbeddings())))
    with TestClient(app) as client:
        pid = client.post('/api/projects', json={'name':'test','agent':'codex'}).json()['id']
        other = client.post('/api/projects', json={'name':'other','agent':'codex'}).json()['id']
        base = f'/api/projects/{pid}'
        files = [client.post(base + '/files?kind=corpus', files={'file':(f'{n}.txt', b'The doctor works in a hospital.')}).json() for n in range(2)]
        assert len(client.get(base + '/files').json()) == 2
        assert client.delete(f'/api/projects/{other}/files/{files[0]["id"]}').status_code == 400
        assert client.request('DELETE', base, json={'name':'wrong'}).status_code == 400
        assert client.request('DELETE', base, json={'name':'test'}, headers={'Origin':'https://evil.example'}).status_code == 403
        assert client.delete(base + '/files/' + files[0]['id']).status_code == 200
        assert [f['id'] for f in client.get(base + '/files').json()] == [files[1]['id']]
        started, release = threading.Event(), threading.Event()

        def slow_extract(path):
            started.set()
            assert release.wait(5)
            return ['The doctor works in a hospital.']

        monkeypatch.setattr('transmux.app.extract', slow_extract)
        with ThreadPoolExecutor() as pool:
            uploading = pool.submit(client.post, base + '/files?kind=corpus', files={'file':('pending.txt', b'document')})
            try:
                assert started.wait(5)
                assert client.request('DELETE', base, json={'name':'test'}).status_code == 409
                assert client.delete(base + '/files/' + files[1]['id']).status_code == 409
            finally:
                release.set()
            assert uploading.result().status_code == 201
        assert client.request('DELETE', base, json={'name':'test'}).status_code == 200
        assert client.get(base).status_code == 400
        assert client.get(f'/api/projects/{other}').status_code == 200
        assert not list((tmp_path / '.deleting').iterdir())


@pytest.mark.parametrize('state', ['queued','running'])
def test_corpus_cannot_be_deleted_while_tasks_are_active(tmp_path, state):
    store = Store(tmp_path)
    pid = store.create_project('test','codex')['id']
    fid = add_source(store, pid, 'corpus')
    task = store.enqueue(pid,'rag',{})
    store.execute('UPDATE jobs SET state=? WHERE id=?', (state,task['id']))
    with pytest.raises(ConfigConflict):
        store.delete_corpus(pid, fid)
    assert store.file(pid, fid)
    store.db.close()
