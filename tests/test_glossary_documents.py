import json

import pytest
from fastapi.testclient import TestClient

from transmux.glossary_documents import matching_entries, markdown
from transmux.store import ConfigConflict
from transmux.v2 import WorkspaceStore, WorkspaceWorker, create_v2_app
from test_v2 import Runner, upload, finished


class GlossaryRunner(Runner):
    async def run_isolated(self, pid, jid, prompt, schema=None):
        if 'entries' in schema['properties']:
            self.calls.append((pid, jid, prompt, schema))
            return {'entries': [{'source': '自动机', 'target': 'automaton', 'context': 'Computer science.',
                                 'quote': '自动机 | automaton | Computer science.'}]}
        return await super().run_isolated(pid, jid, prompt, schema)


def test_upload_normalization_approval_preview_and_revision(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id': 'codex', 'available': True}])
    runner = GlossaryRunner()
    app = create_v2_app(tmp_path, lambda store: WorkspaceWorker(store, runner))
    with TestClient(app) as client:
        pid = client.post('/api/projects', json={'name': 'Glossary', 'agent': 'codex'}).json()['id']
        base = f'/api/projects/{pid}'
        source = upload(client, pid, '自动机 | automaton | Computer science.', 'glossary.docx')
        assert client.post(base + '/messages', json={'kind': 'glossary'}).status_code == 400
        job = client.post(base + '/messages', json={'kind': 'glossary', 'file_ids': [source['id']]}).json()
        result = finished(client, pid, job['id'])
        assert result['state'] == 'succeeded', result['result']
        row = client.get(base + '/reviews').json()[0]
        assert row['kind'] == 'glossary' and row['status'] == 'pending'
        content = client.get(base + f"/files/{row['file_id']}/download").text
        assert '| 自动机 | automaton | Computer science. |' in content
        translation = upload(client, pid)
        assert client.post(base + '/messages', json={'kind': 'translate', 'file_ids': [translation['id']],
                            'glossary_version_id': row['id']}).status_code == 409
        assert client.post(base + f"/reviews/{row['id']}/approve").status_code == 200
        task = client.post(base + '/messages', json={'kind': 'translate', 'file_ids': [translation['id']]}).json()
        assert json.loads(task['payload'])['glossary_version_id'] == row['id']
        done = finished(client, pid, task['id'])
        assert done['state'] == 'succeeded', done['result']
        fid = json.loads(done['result'])['documents'][0]['file_id']
        assert next(r for r in client.get(base + '/reviews').json() if r['file_id'] == fid)['glossary_id'] == row['id']
        revised = client.post(base + '/messages', json={'message': 'Keep the terminology, clarify the context.',
                            'review_id': row['id']}).json()
        assert finished(client, pid, revised['id'])['state'] == 'succeeded'
        rows = client.get(base + '/reviews').json()
        child = next(r for r in rows if r['parent'] == row['id'])
        assert child['status'] == 'pending' and child['version'] == 2
        assert next(r for r in rows if r['id'] == row['id'])['status'] == 'approved'
        assert client.get(base + f"/files/{source['id']}/download").status_code == 200


def test_glossary_selection_and_version_pinning(tmp_path):
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('Selection', 'codex')['id']
    other = store.create_project('Other', 'codex')['id']
    pair = [{'source': '自动机', 'target': 'automaton', 'context': ''}]
    zero = store.enqueue(pid, 'translate', {})
    assert json.loads(zero['payload'])['glossary_version_id'] is None
    first = store.create_glossary(pid, 'One', pair)
    with pytest.raises(ConfigConflict):
        store.enqueue(pid, 'translate', {'glossary_version_id': first['id']})
    store.approve_review(pid, first['id'])
    one = store.enqueue(pid, 'translate', {})
    assert json.loads(one['payload'])['glossary_version_id'] == first['id']
    child = store.create_glossary(pid, 'One', [{'source': '自动机', 'target': 'automata', 'context': 'Plural.'}], parent=first['id'])
    store.approve_review(pid, child['id'])
    assert store.approved_glossaries(pid)[0]['id'] == child['id']
    assert json.loads(store.rows('SELECT payload FROM jobs WHERE id=?', (one['id'],))[0]['payload'])['glossary_version_id'] == first['id']
    second = store.create_glossary(pid, 'Two', pair)
    store.approve_review(pid, second['id'])
    with pytest.raises(ValueError, match='多份'):
        store.enqueue(pid, 'translate', {})
    chosen = store.enqueue(pid, 'translate', {'glossary_version_id': first['id']})
    assert json.loads(chosen['payload'])['glossary_version_id'] == first['id']
    with pytest.raises(ValueError, match='不存在'):
        store.enqueue(other, 'translate', {'glossary_version_id': first['id']})
    store.db.close()


def test_only_matching_entries_reach_translation_and_review(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id': 'codex', 'available': True}])
    runner = Runner()
    app = create_v2_app(tmp_path, lambda store: WorkspaceWorker(store, runner))
    with TestClient(app) as client:
        pid = client.post('/api/projects', json={'name': 'Bounded glossary', 'agent': 'codex'}).json()['id']
        rows = [{'source': f'UNRELATED_TERM_{i}', 'target': 'irrelevant', 'context': 'Large evidence. ' * 50} for i in range(1500)]
        rows.append({'source': 'Germany', 'target': 'Germany', 'context': 'Country name.'})
        review = client.portal.call(app.state.store.create_glossary, pid, 'Large glossary', rows)
        client.post(f"/api/projects/{pid}/reviews/{review['id']}/approve")
        source = upload(client, pid)
        task = client.post(f'/api/projects/{pid}/messages', json={'kind': 'translate', 'file_ids': [source['id']]}).json()
        result = finished(client, pid, task['id'])
        assert result['state'] == 'succeeded', result['result']
        assert len(runner.calls) == 2
        for _, _, prompt, _ in runner.calls:
            assert 'Approved glossary entries for this batch' in prompt
            assert 'Country name.' in prompt
            assert 'UNRELATED_TERM_' not in prompt
        # An approved but irrelevant glossary adds no template or entries.
        selected = client.portal.call(app.state.store.create_glossary, pid, 'Irrelevant', rows[:1])
        client.post(f"/api/projects/{pid}/reviews/{selected['id']}/approve")
        runner.calls.clear()
        task = client.post(f'/api/projects/{pid}/messages', json={'kind': 'translate', 'file_ids': [source['id']],
                          'glossary_version_id': selected['id']}).json()
        assert finished(client, pid, task['id'])['state'] == 'succeeded'
        assert all('Approved glossary entries for this batch' not in call[2] for call in runner.calls)


def test_matching_and_markdown_escape():
    rows = [{'source': 'AI', 'target': '人工智能', 'context': '<script>|test</script>'},
            {'source': '自动机', 'target': 'automaton', 'context': ''}]
    assert matching_entries(rows, ['mail and aileron']) == []
    assert matching_entries(rows, ['Use ai and 自动机.']) == rows
    result = markdown(rows)
    assert '<script>' not in result and '&#124;' in result


def test_invented_pair_never_becomes_a_reviewable_glossary(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id': 'codex', 'available': True}])
    app = create_v2_app(tmp_path, lambda store: WorkspaceWorker(store, GlossaryRunner()))
    with TestClient(app) as client:
        pid = client.post('/api/projects', json={'name': 'Evidence', 'agent': 'codex'}).json()['id']
        source = upload(client, pid, 'This is monolingual prose, not a supplied bilingual mapping.')
        job = client.post(f'/api/projects/{pid}/messages', json={'kind': 'glossary', 'file_ids': [source['id']]}).json()
        assert finished(client, pid, job['id'])['state'] == 'needs_attention'
        assert not client.get(f'/api/projects/{pid}/reviews').json()
