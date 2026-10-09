import json

import pytest
from fastapi.testclient import TestClient

from transmux.v2 import WorkspaceStore, WorkspaceWorker, create_v2_app
from test_v2 import finished, upload
from test_glossary_documents import GlossaryRunner


class ConsistencyRunner(GlossaryRunner):
    bad_evidence = False

    async def run_isolated(self, pid, jid, prompt, schema=None):
        if schema and 'terms' in schema['properties'] and 'findings' in schema['properties']:
            self.calls.append((pid, jid, prompt, schema))
            data = json.loads(prompt.rsplit('\n', 1)[1])
            group = data['groups'][0]
            return {'findings': [{'group_id': group['id'], 'category': 'mistranslation',
                'source_quote': 'invented evidence' if self.bad_evidence else group['original'],
                'target_quote': group['translation'], 'explanation': 'Check the country.',
                'suggestion': 'Use France.'}], 'terms': []}
        return await super().run_isolated(pid, jid, prompt, schema)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id': 'codex', 'available': True}])
    runner = ConsistencyRunner()
    def factory(store):
        runner.store = store
        return WorkspaceWorker(store, runner)
    app = create_v2_app(tmp_path, factory)
    with TestClient(app) as client:
        pid = client.post('/api/projects', json={'name': 'Agent', 'agent': 'codex'}).json()['id']
        yield client, pid, runner, app.state.store


def test_consistency_uses_translation_alignment_and_requires_human_review(workspace):
    client, pid, runner, store = workspace
    source = upload(client, pid, 'Paris is in France.')
    job = client.post(f'/api/projects/{pid}/messages', json={'kind': 'translate', 'file_ids': [source['id']]}).json()
    result = finished(client, pid, job['id'])
    fid = json.loads(result['result'])['documents'][0]['file_id']
    before = client.get(f'/api/projects/{pid}/files/{fid}/download').content
    # Consistency checks may inspect pending translations, without approving them.
    response = client.post(f'/api/projects/{pid}/messages', json={'kind': 'consistency', 'file_ids': [fid]})
    assert response.status_code == 202, response.text
    result = finished(client, pid, response.json()['id'])
    assert result['state'] == 'succeeded', result
    report_id = json.loads(result['result'])['file_id']
    review = next(r for r in client.get(f'/api/projects/{pid}/reviews').json() if r['file_id'] == report_id)
    assert review['kind'] == 'consistency' and review['status'] == 'pending'
    report = client.get(f'/api/projects/{pid}/files/{report_id}/preview').json()['content']
    assert 'Paris is in France.' in report and 'Paris is in Germany.' in report
    assert client.post(f"/api/projects/{pid}/reviews/{review['id']}/approve").status_code == 200
    assert next(r for r in client.get(f'/api/projects/{pid}/reviews').json() if r['file_id'] == fid)['status'] == 'pending'
    assert client.get(f'/api/projects/{pid}/files/{fid}/download').content == before


def test_external_consistency_scope_and_evidence_validation(workspace):
    client, pid, runner, store = workspace
    source = upload(client, pid, '巴黎在法国。', 'source.docx')
    target = upload(client, pid, 'Paris is in Germany.', 'target.docx')
    url = f'/api/projects/{pid}/messages'
    assert client.post(url, json={'kind': 'consistency', 'file_ids': [target['id']]}).status_code == 400
    assert client.post(url, json={'kind': 'consistency', 'file_ids': [target['id']], 'consistency_source_id': target['id']}).status_code == 400
    other = client.post('/api/projects', json={'name': 'Other', 'agent': 'codex'}).json()['id']
    foreign = upload(client, other)
    assert client.post(url, json={'kind': 'consistency', 'file_ids': [target['id']], 'consistency_source_id': foreign['id']}).status_code == 400
    runner.bad_evidence = True
    job = client.post(url, json={'kind': 'consistency', 'file_ids': [target['id']], 'consistency_source_id': source['id']}).json()
    result = finished(client, pid, job['id'])
    assert result['state'] == 'needs_attention'
    assert not [r for r in client.get(f'/api/projects/{pid}/reviews').json() if r['kind'] == 'consistency']


def test_existing_reports_and_layout_get_pending_reviews_once(tmp_path):
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('Existing', 'codex')['id']
    for kind, name in [('report', 'report.md'), ('typeset', 'layout.docx'), ('typeset', 'manifest.json')]:
        path = store.workspace(pid) / name
        path.write_text('fixture')
        store.add_file(pid, name, kind, path)
    store.db.close()
    store = WorkspaceStore(tmp_path)
    rows = store.rows('SELECT * FROM human_reviews')
    assert sorted(r['kind'] for r in rows) == ['layout', 'report']
    assert all(r['status'] == 'pending' for r in rows)
    store.approve_review(pid, rows[0]['id'])
    store.db.close()
    store = WorkspaceStore(tmp_path)
    assert len(store.rows('SELECT * FROM human_reviews')) == 2
    assert store.review(pid, rows[0]['id'])['status'] == 'approved'
    store.db.close()


def test_missing_task_file_is_rejected_before_enqueue(workspace):
    client, pid, runner, store = workspace
    source = upload(client, pid, 'A document for translation.')
    client.portal.call(lambda: store.download_path(pid, store.file(pid, source['id'])).unlink())
    response = client.post(f'/api/projects/{pid}/messages', json={'kind': 'translate', 'file_ids': [source['id']]})
    assert response.status_code == 400
    assert client.get(f'/api/projects/{pid}/jobs').json() == []
