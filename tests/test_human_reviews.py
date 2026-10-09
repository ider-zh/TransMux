import asyncio
import json

import pytest
from docx import Document
from fastapi.testclient import TestClient

from transmux.store import Store, ConfigConflict
from transmux.v2 import WorkspaceStore, WorkspaceWorker, create_v2_app
from test_v2 import Runner, upload, finished


def test_style_versions_selection_and_pinned_snapshot(tmp_path):
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('Styles', 'codex')['id']
    first = store.create_style(pid, 'Formal', '# Style\nUse concise English.')
    assert not store.approved_styles(pid)
    generic = store.enqueue(pid, 'translate', {})
    assert json.loads(generic['payload'])['style_version_id'] == 'generic'
    with pytest.raises(ConfigConflict):
        store.enqueue(pid, 'translate', {'style_version_id': first['id']})
    store.approve_review(pid, first['id'])
    task = store.enqueue(pid, 'translate', {})
    snapshot = json.loads(task['payload'])
    assert snapshot['style_version_id'] == first['id']
    newer = store.create_style(pid, first['name'], '# Style\nUse detailed English.', parent=first['id'])
    assert store.approved_styles(pid)[0]['id'] == first['id']
    store.approve_review(pid, newer['id'])
    assert store.approved_styles(pid)[0]['id'] == newer['id']
    assert snapshot['_style_content'] == first['content']
    second = store.create_style(pid, 'Narrative', '# Style\nUse a narrative tone.')
    store.approve_review(pid, second['id'])
    with pytest.raises(ValueError, match='选择'):
        store.enqueue(pid, 'translate', {})
    chosen = store.enqueue(pid, 'translate', {'style_version_id': newer['id']})
    assert json.loads(chosen['payload'])['_style_content'] == newer['content']
    store.db.close()


def test_historical_migration_pending_idempotent_and_preserves_files(tmp_path):
    old = Store(tmp_path)
    pid = old.create_project('Historical', 'codex')['id']
    path = old.workspace(pid) / 'outputs' / 'old.docx'
    doc = Document()
    doc.add_paragraph('Historical document.')
    doc.save(path)
    fid = old.add_file(pid, path.name, 'output', path)
    data = path.read_bytes()
    style = (old.workspace(pid) / 'style.md').read_bytes()
    old.db.close()
    for _ in range(2):
        store = WorkspaceStore(tmp_path)
        rows = store.rows('SELECT * FROM human_reviews WHERE project=?', (pid,))
        assert len(rows) == 2 and all(r['status'] == 'pending' for r in rows)
        assert store.review_for_file(pid, fid)['status'] == 'pending'
        assert path.read_bytes() == data
        assert (store.workspace(pid) / 'style.md').read_bytes() == style
        with pytest.raises(ConfigConflict):
            store.require_approved_input(pid, fid)
        store.db.close()


def test_new_workspace_stays_without_learned_styles_after_restart(tmp_path):
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('Fresh', 'codex')['id']
    store.db.close()
    store = WorkspaceStore(tmp_path)
    assert not store.rows('SELECT * FROM human_reviews WHERE project=?', (pid,))
    store.db.close()


def test_style_extraction_never_updates_terminology_or_active_style(tmp_path):
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('Extraction', 'codex')['id']
    work = store.workspace(pid)
    path = work / 'sources' / 'source.txt'
    path.write_text('The doctor describes the experiment clearly and precisely.')
    fid = store.add_file(pid, path.name, 'source', path)
    before = {name: (work / name).read_bytes() for name in ['style.md', 'terms.json', 'mappings.json', 'people.json']}
    runner = Runner()
    worker = WorkspaceWorker(store, runner)
    for _ in range(2):
        task = store.enqueue(pid, 'style', {'file_ids': [fid]})
        asyncio.run(worker.execute(task))
        assert store.rows('SELECT state FROM jobs WHERE id=?', (task['id'],))[0]['state'] == 'succeeded'
    rows = store.rows("SELECT * FROM human_reviews WHERE project=? AND kind='style'", (pid,))
    assert len(rows) == 2 and len({r['root'] for r in rows}) == 2
    assert all(r['status'] == 'pending' for r in rows)
    assert before == {name: (work / name).read_bytes() for name in before}
    observation_calls = [c for c in runner.calls if 'observations' in c[3]['properties']]
    assert len({c[1] for c in observation_calls}) == 1
    assert set(observation_calls[0][3]['properties']) == {'observations'}
    store.db.close()


def test_approval_scope_chat_revision_and_downstream_gate(tmp_path, monkeypatch):
    from test_layout import fake_pdf
    monkeypatch.setattr('transmux.layout.render_pdf', fake_pdf)
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id': 'codex', 'available': True}])
    class EditRunner(Runner):
        async def run_isolated(self, pid, jid, prompt, schema=None):
            if 'content' in schema['properties']:
                return {'content': '# Revised style\nUse concise, natural English.'}
            if 'answer' in schema['properties'] and 'Replace Germany with France.' in prompt:
                data = json.loads(prompt.split('\n')[-1])
                return {'answer': 'Correction drafted.', 'edits': [{'file_id': data['files'][0]['id'],
                        'original': 'Germany', 'replacement': 'France'}], 'formatting': []}
            return await super().run_isolated(pid, jid, prompt, schema)
    app = create_v2_app(tmp_path, lambda store: WorkspaceWorker(store, EditRunner()))
    with TestClient(app) as client:
        pid = client.post('/api/projects', json={'name': 'Review', 'agent': 'codex'}).json()['id']
        other = client.post('/api/projects', json={'name': 'Other', 'agent': 'codex'}).json()['id']
        base = f'/api/projects/{pid}'
        style = client.portal.call(app.state.store.create_style, pid, 'Formal', '# Style\nUse formal English.')
        rid = style['id']
        assert client.post(f'/api/projects/{other}/reviews/{rid}/approve').status_code == 400
        assert client.get(base + f'/files/{style["file_id"]}/download').status_code == 200
        # A casual chat message never approves a version.
        task = client.post(base + '/messages', json={'message': 'ok'}).json()
        assert finished(client, pid, task['id'])['state'] == 'succeeded'
        assert client.get(base + f'/reviews/{rid}').json()['status'] == 'pending'
        assert client.post(base + f'/reviews/{rid}/approve').status_code == 200
        task = client.post(base + '/messages', json={'message': 'Make it concise.', 'review_id': rid}).json()
        assert finished(client, pid, task['id'])['state'] == 'succeeded'
        versions = client.get(base + '/reviews').json()
        assert versions[0]['parent'] == rid and versions[0]['status'] == 'pending'
        assert versions[1]['status'] == 'approved'
        source = upload(client, pid)
        task = client.post(base + '/messages', json={'kind': 'translate', 'file_ids': [source['id']]}).json()
        assert json.loads(task['payload'])['style_version_id'] == rid
        result = finished(client, pid, task['id'])
        assert result['state'] == 'succeeded'
        fid = json.loads(result['result'])['documents'][0]['file_id']
        review = next(r for r in client.get(base + '/reviews').json() if r['file_id'] == fid)
        assert review['style_id'] == rid
        for kind in ['layout', 'factcheck']:
            assert client.post(base + '/messages', json={'kind': kind, 'file_ids': [fid]}).status_code == 409
        assert client.post(base + f'/reviews/{review["id"]}/approve').status_code == 200
        assert client.post(base + f'/reviews/{versions[0]["id"]}/approve').status_code == 200
        # New style approval does not invalidate the existing translation.
        assert client.get(base + f'/reviews/{review["id"]}').json()['status'] == 'approved'
        assert client.post(base + '/messages', json={'kind': 'layout'}).status_code == 400
        layout = client.post(base + '/messages', json={'kind': 'layout', 'file_ids': [fid]}).json()
        assert json.loads(layout['payload'])['file_ids'] == [fid]
        assert finished(client, pid, layout['id'])['state'] == 'succeeded'
        external = client.post(base + '/messages', json={'kind': 'layout', 'file_ids': [source['id']]}).json()
        assert finished(client, pid, external['id'])['state'] == 'succeeded'
        revised = client.post(base + '/messages', json={'message': 'Replace Germany with France.', 'review_id': review['id']}).json()
        result = finished(client, pid, revised['id'])
        assert result['state'] == 'succeeded', result['result']
        new_file = json.loads(result['result'])['file_ids'][0]
        new_review = next(r for r in client.get(base + '/reviews').json() if r['file_id'] == new_file)
        assert new_review['parent'] == review['id'] and new_review['status'] == 'pending'
        assert new_review['style_id'] == rid
        assert client.get(base + f'/reviews/{review["id"]}').json()['status'] == 'approved'
        assert client.post(base + '/messages', json={'kind': 'layout', 'file_ids': [new_file]}).status_code == 409


def test_review_integrity_and_project_deletion(tmp_path):
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('Review cleanup', 'codex')['id']
    other = store.create_project('Keep', 'codex')['id']
    row = store.create_style(pid, 'Style', '# Style\nUse concise English.')
    store.approve_review(pid, row['id'])
    file = store.file(pid, row['file_id'])
    store.download_path(pid, file).write_text('Changed after approval.')
    with pytest.raises(ConfigConflict, match='变化'):
        store.require_approved_input(pid, row['file_id'])
    store.delete_project(pid, 'Review cleanup')
    assert not store.rows('SELECT * FROM human_reviews WHERE project=?', (pid,))
    assert not store.rows('SELECT * FROM review_migrations WHERE project=?', (pid,))
    assert store.project(other)['name'] == 'Keep'
    store.db.close()
