import json

import pytest
from fastapi.testclient import TestClient

from transmux.v2 import WorkspaceStore, create_v2_app


def test_selected_rules_generate_markdown_and_preserve_evidence(tmp_path):
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('Rules', 'codex')['id']
    rules = [
        {'text': 'Use concise sentences.', 'evidence': [{'source': 'Reference.docx', 'paragraph': 2, 'quote': 'Short sentences improve clarity.'}]},
        {'text': 'Prefer active voice.', 'evidence': [{'source': 'Reference.docx', 'paragraph': 4, 'quote': 'We evaluate the proposed method.'}]},
    ]
    review = store.create_style_rules(pid, 'Style', rules)
    path = store.download_path(pid, store.file(pid, review['file_id']))
    assert path.suffix == '.json' and not list(path.parent.glob('*.md'))
    app = create_v2_app(tmp_path)
    with TestClient(app) as client:
        url = f"/api/projects/{pid}/reviews/{review['id']}/approve"
        for payload in ({}, {'selected_rule_ids': []}, {'selected_rule_ids': ['missing']}, {'selected_rule_ids': ['1', '1']}):
            assert client.post(url, json=payload).status_code == 400
        assert client.post(url, json={'selected_rule_ids': ['2']}).status_code == 200
        assert client.post(url, json={'selected_rule_ids': ['2']}).status_code == 200
        approved = store.review(pid, review['id'])
        assert approved['status'] == 'approved'
        assert 'Prefer active voice.' in approved['content']
        assert 'Use concise sentences.' not in approved['content']
        entries = json.loads(approved['entries'])
        assert entries[0]['selected'] is False and entries[1]['selected'] is True
        assert entries[1]['evidence'] == rules[1]['evidence']
        final = store.download_path(pid, store.file(pid, review['file_id']))
        assert final.suffix == '.md' and final.read_text() == approved['content']
        assert path.is_file()  # The original candidate remains available for audit.
        store.require_approved_input(pid, review['file_id'])
        other = store.create_project('Other', 'codex')['id']
        assert client.post(f"/api/projects/{other}/reviews/{review['id']}/approve", json={'selected_rule_ids': ['1']}).status_code == 400
    reopened = WorkspaceStore(tmp_path)
    assert reopened.review(pid, review['id'])['entries'] == approved['entries']


async def test_structured_revision_keeps_evidence_and_requires_new_selection(tmp_path):
    from transmux.v2 import WorkspaceWorker
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('Revision', 'codex')['id']
    old = store.create_style_rules(pid, 'Style', [{'text': 'Use concise sentences.', 'evidence': [
        {'source': 'reference.docx', 'paragraph': 3, 'quote': 'We report our results.'}]}])
    class Runner:
        async def run_isolated(self, pid, jid, prompt, schema):
            return {'rules': [{'text': 'Prefer short, direct sentences.', 'evidence_ids': [1, 2]}]}
    worker = WorkspaceWorker(store, Runner())
    payload = {'review_id': old['id'], 'message': 'Make the sentences direct.'}
    job = store.enqueue(pid, 'chat', payload)
    result = await worker.chat(job, payload, tmp_path)
    new = store.review(pid, result['review_id'])
    assert new['parent'] == old['id'] and new['status'] == 'pending'
    entries = json.loads(new['entries'])
    assert entries[0]['evidence'][0]['source'] == 'reference.docx'
    assert entries[0]['evidence'][1]['quote'] == payload['message']
    assert new['content'] is None


def test_pending_legacy_style_recovers_saved_evidence(tmp_path):
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('History', 'codex')['id']
    job = store.enqueue(pid, 'style', {})
    review = store.create_style(pid, 'Old', '# Style\n\nUse concise sentences.', job['id'])
    path = store.workspace(pid) / 'runs' / job['id'] / 'style-rule-evidence.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{'text': 'Use concise sentences.', 'evidence': [
        {'source': 'reference.docx', 'paragraph': 1, 'quote': 'We report results.'}]}]))
    store.db.close()
    reopened = WorkspaceStore(tmp_path)
    row = reopened.review(pid, review['id'])
    assert json.loads(row['entries'])[0]['evidence'][0]['source'] == 'reference.docx'
    assert row['status'] == 'pending'
    with pytest.raises(ValueError):
        reopened.approve_review(pid, review['id'])
    assert reopened.approve_review(pid, review['id'], ['1'])['status'] == 'approved'
