import json
import sys

import pytest

from transmux.agents import AgentRunner
from transmux.store import ConfigConflict, Store
from transmux.usage import UsageCall, summary, terminal_usage
from transmux.v2 import WorkspaceStore


def project_job(store, agent='codex'):
    pid = store.create_project('Usage', agent)['id']
    job = store.enqueue(pid, 'chat', {'message': 'Hello'})
    store.execute('UPDATE jobs SET usage_tracking=1 WHERE id=?', (job['id'],))
    return pid, job['id']


def test_codex_cached_tokens_multiple_calls_and_duplicate_events(tmp_path):
    store = Store(tmp_path)
    pid, jid = project_job(store)
    event = {'type': 'turn.completed', 'usage': {'input_tokens': 1_000_000,
             'cached_input_tokens': 200_000, 'output_tokens': 100_000, 'reasoning_output_tokens': 20_000}}
    call = UsageCall(store, pid, jid, 'codex')
    call.consume(event)
    call.consume(event)
    UsageCall(store, pid, jid, 'codex').consume(event)
    result = summary(store, pid)
    assert result['total']['total_tokens'] == 2_200_000
    assert result['total']['estimated_cny'] == 52.8
    assert result['jobs'][jid]['calls'] == 2
    assert result['total']['incomplete_calls'] == 0
    store.db.close()


def test_codebuddy_prefers_final_totals_not_streamed_duplicates(tmp_path):
    store = Store(tmp_path)
    pid, jid = project_job(store, 'codebuddy')
    call = UsageCall(store, pid, jid, 'codebuddy')
    event = {'type': 'assistant', 'message': {'id': 'message-1', 'usage': {
        'input_tokens': 100, 'cache_creation_input_tokens': 100, 'output_tokens': 10}}}
    call.consume(event)
    call.consume(event)
    assert summary(store, pid)['total']['input_tokens'] == 100
    assert summary(store, pid)['total']['incomplete_calls'] == 1
    final = {'type': 'result', 'usage': {'input_tokens': 150, 'output_tokens': 20}, 'modelUsage': {
        'hy3': {'inputTokens': 10, 'cacheCreationInputTokens': 100, 'cacheReadInputTokens': 40, 'outputTokens': 20}}}
    call.consume(final)
    call.consume(final)
    data = summary(store, pid)['total']
    assert data['input_tokens'] == 150 and data['cached_input_tokens'] == 40
    assert data['output_tokens'] == 20 and data['estimated_cny'] == .00428
    assert data['incomplete_calls'] == 0
    assert terminal_usage({'type': 'result', 'usage': {'input_tokens': 150, 'cache_read_input_tokens': 40, 'output_tokens': 20}}, 'codebuddy')['input_tokens'] == 150
    store.db.close()


async def test_usage_survives_failed_process_and_unreported_is_not_zero(tmp_path, monkeypatch):
    store = Store(tmp_path / 'data')
    pid, jid = project_job(store)
    script = tmp_path / 'cli.py'
    script.write_text('import sys,json\nsys.stdin.read()\nprint(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":50}}))\nsys.exit(1)')
    monkeypatch.setattr('transmux.agents.command', lambda *args: [sys.executable, str(script)])
    with pytest.raises(RuntimeError):
        await AgentRunner(store).run_isolated(pid, jid, 'hello')
    UsageCall(store, pid, jid, 'codex')  # A second call failed before reporting usage.
    data = summary(store, pid)['total']
    assert data['total_tokens'] == 150 and data['estimated_cny'] == .007
    assert data['calls'] == 2 and data['reported_calls'] == 1 and data['incomplete_calls'] == 1
    store.db.close()
    store = Store(tmp_path / 'data')
    assert summary(store, pid)['total'] == data
    store.db.close()


def test_historical_backfill_is_partial_idempotent_and_isolated(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Old', 'codex')['id']
    jid = store.enqueue(pid, 'chat', {})['id']
    store.event(pid, jid, 'agent', json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 5}}))
    store.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (jid,))
    other = store.create_project('Other', 'codebuddy')['id']
    store.db.close()
    for _ in range(2):
        store = Store(tmp_path)
        data = summary(store, pid)['total']
        assert data['total_tokens'] == 25 and data['incomplete_calls'] == 1
        assert data['untracked_jobs'] == 1
        assert summary(store, other)['total']['total_tokens'] == 0
        store.db.close()
    store = Store(tmp_path)
    store.delete_project(pid, 'Old')
    assert not store.rows('SELECT * FROM agent_usage')
    store.db.close()


def test_ignore_preserves_file_and_cannot_supply_translation_resource(tmp_path):
    store = WorkspaceStore(tmp_path)
    pid = store.create_project('Review', 'codex')['id']
    review = store.create_glossary(pid, 'Terms', [{'source': '猫', 'target': 'cat', 'context': ''}])
    path = store.download_path(pid, store.file(pid, review['file_id']))
    before = path.read_bytes()
    assert store.ignore_review(pid, review['id'])['status'] == 'ignored'
    assert path.read_bytes() == before
    with pytest.raises(ConfigConflict):
        store.approve_review(pid, review['id'])
    with pytest.raises(ConfigConflict):
        store.enqueue(pid, 'translate', {'glossary_version_id': review['id']})
    other = store.create_project('Other', 'codex')['id']
    with pytest.raises(ValueError):
        store.ignore_review(other, review['id'])
    store.db.close()
    store = WorkspaceStore(tmp_path)
    assert store.review(pid, review['id'])['status'] == 'ignored'
    store.ignore_review(pid, review['id'], restore=True)
    assert store.approve_review(pid, review['id'])['status'] == 'approved'
    with pytest.raises(ConfigConflict):
        store.ignore_review(pid, review['id'])
    store.db.close()
