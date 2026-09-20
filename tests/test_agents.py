import asyncio
import sys

import pytest

from transmux.agents import AgentRunner
from transmux.jobs import REVIEW
from transmux.store import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "data")
    pid = store.create_project("Adapter", "codex")["id"]
    jid = store.enqueue(pid, "chat", {"message":"test"})["id"]
    (store.workspace(pid) / "runs" / jid).mkdir()
    yield store, pid, jid, tmp_path
    store.db.close()


async def test_codex_stream_and_session_resume(setup, monkeypatch):
    store, pid, jid, root = setup
    script = root / 'fake.py'
    script.write_text('''import sys, json
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"session-123"}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":'{"passed":true,"issues":[]}'}}), flush=True)
print(json.dumps({"type":"turn.completed"}), flush=True)
''')
    seen = []

    def fake_command(agent, session, schema, model):
        seen.append(session)
        return [sys.executable, str(script)]

    monkeypatch.setattr('transmux.agents.command', fake_command)
    runner = AgentRunner(store)
    assert await runner.run(pid, jid, 'review', REVIEW) == {"passed": True, "issues": []}
    await runner.run(pid, jid, 'review again', REVIEW)
    assert seen == [None, 'session-123']
    assert store.project(pid)['session'] == 'session-123'
    assert len(store.rows("SELECT * FROM events WHERE kind='agent'")) == 6


async def test_codebuddy_structured_result_and_error(setup, monkeypatch):
    store, pid, jid, root = setup
    script = root / 'buddy.py'
    script.write_text('''import sys, json
sys.stdin.read()
print(json.dumps({"type":"system","session_id":"buddy-session"}), flush=True)
print(json.dumps({"type":"result","structured_output":{"passed":True,"issues":[]}}), flush=True)
''')
    monkeypatch.setattr('transmux.agents.command', lambda *args: [sys.executable, str(script)])
    runner = AgentRunner(store)
    assert (await runner.run(pid, jid, 'review', REVIEW))['passed'] is True
    assert store.project(pid)['session'] == 'buddy-session'
    script.write_text('''import sys, json
sys.stdin.read()
print(json.dumps({"type":"result","is_error":True,"result":"permission denied"}), flush=True)
''')
    with pytest.raises(RuntimeError, match='permission denied'):
        await runner.run(pid, jid, 'review', REVIEW)


async def test_timeout_terminates_child(setup, monkeypatch):
    store, pid, jid, root = setup
    script = root / 'sleep.py'
    marker = root / 'still-running'
    script.write_text('import time\nfrom pathlib import Path\ntime.sleep(2)\nPath(' + repr(str(marker)) + ').touch()')
    monkeypatch.setattr('transmux.agents.command', lambda *args: [sys.executable, str(script)])
    monkeypatch.setenv('TRANSMUX_AGENT_TIMEOUT', '1')
    with pytest.raises(asyncio.TimeoutError):
        await AgentRunner(store).run(pid, jid, 'timeout')
    await asyncio.sleep(1.2)
    assert not marker.exists()


@pytest.mark.parametrize('fresh_fails', [False, True])
async def test_codebuddy_context_limit_retries_current_structured_call_once(tmp_path, monkeypatch, fresh_fails):
    store = Store(tmp_path/'data')
    pid = store.create_project('Buddy', 'codebuddy', model='selected-model')['id']
    store.execute('UPDATE projects SET session=? WHERE id=?', ('full-session', pid))
    jid = store.enqueue(pid, 'translate', {'_model':'task-model'})['id']
    (store.workspace(pid)/'runs'/jid).mkdir()
    before_style = (store.workspace(pid)/'style.md').read_bytes()
    script = tmp_path/'buddy.py'
    script.write_text('''import sys,json
prompt=sys.stdin.read()
fail=sys.argv[1]=='full-session' or sys.argv[2]=='True'
print(json.dumps({'type':'system','session_id':'full-session' if sys.argv[1]=='full-session' else 'fresh-session'}))
if fail:
 print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'errors':['400 prompt is too long: 100001 tokens > 100000 maximum']}))
else:
 print(json.dumps({'type':'result','structured_output':{'passed':True,'issues':[]}}))
''')
    seen = []

    def cli(agent, session, schema, model):
        seen.append((session, model, schema.read_text()))
        return [sys.executable, str(script), session or 'new', str(fresh_fails)]

    monkeypatch.setattr('transmux.agents.command', cli)
    runner = AgentRunner(store)
    if fresh_fails:
        with pytest.raises(RuntimeError, match='已停止重试'):
            await runner.run(pid, jid, 'Self-contained review input', REVIEW)
        assert store.project(pid)['session'] is None
    else:
        assert await runner.run(pid, jid, 'Self-contained review input', REVIEW) == {'passed':True,'issues':[]}
        assert store.project(pid)['session'] == 'fresh-session'
    assert [call[0] for call in seen] == ['full-session', None]
    assert all(call[1] == 'task-model' for call in seen)
    assert seen[0][2] == seen[1][2]
    assert (store.workspace(pid)/'style.md').read_bytes() == before_style
    assert any('最多一次' in event['text'] for event in store.rows("SELECT text FROM events WHERE kind='progress'"))
    store.db.close()


@pytest.mark.parametrize('session,schema', [('full-session', None), (None, REVIEW)])
async def test_context_limit_does_not_replay_chat_or_loop_on_fresh_input(tmp_path, monkeypatch, session, schema):
    store = Store(tmp_path/'data')
    pid = store.create_project('Buddy', 'codebuddy')['id']
    store.execute('UPDATE projects SET session=? WHERE id=?', (session, pid))
    jid = store.enqueue(pid, 'chat', {'message':'test'})['id']
    (store.workspace(pid)/'runs'/jid).mkdir()
    script = tmp_path/'buddy.py'
    script.write_text('import sys,json\nsys.stdin.read()\nprint(json.dumps({"type":"result","is_error":True,"errors_info":[{"code":11115,"details":"400 prompt is too long: 100001 tokens > 100000 maximum"}]}))')
    seen = []

    def cli(*args):
        seen.append(args[1])
        return [sys.executable, str(script)]

    monkeypatch.setattr('transmux.agents.command', cli)
    with pytest.raises(RuntimeError, match='未自动重放'):
        await AgentRunner(store).run(pid, jid, 'test', schema)
    assert seen == [session]
    assert store.project(pid)['session'] is None
    store.db.close()


async def test_previously_failed_codebuddy_session_starts_fresh_without_resending(tmp_path, monkeypatch):
    store = Store(tmp_path/'data')
    pid = store.create_project('Buddy', 'codebuddy')['id']
    store.execute('UPDATE projects SET session=? WHERE id=?', ('full-session', pid))
    old = store.enqueue(pid, 'translate', {})
    store.execute("UPDATE jobs SET state='failed',result=? WHERE id=?",
                  ('codebuddy 调用失败 (exit=0): full-session: 400 prompt is too long',old['id']))
    jid = store.enqueue(pid, 'translate', {})['id']
    (store.workspace(pid)/'runs'/jid).mkdir()
    script = tmp_path/'buddy.py'
    script.write_text('import sys,json\nsys.stdin.read()\nprint(json.dumps({"type":"result","session_id":"new-session","structured_output":{"passed":True,"issues":[]}}))')
    seen = []

    def cli(*args):
        seen.append(args[1])
        return [sys.executable, str(script)]

    monkeypatch.setattr('transmux.agents.command', cli)
    assert (await AgentRunner(store).run(pid, jid, 'test', REVIEW))['passed']
    assert seen == [None]
    assert store.project(pid)['session'] == 'new-session'
    assert store.rows('SELECT state FROM jobs WHERE id=?',(old['id'],))[0]['state'] == 'failed'
    store.db.close()


async def test_isolated_extraction_does_not_resume_or_replace_chat_session(setup, monkeypatch):
    store, pid, jid, root = setup
    store.execute('UPDATE projects SET session=? WHERE id=?', ('chat-session', pid))
    script = root / 'isolated.py'
    script.write_text('import sys,json\nsys.stdin.read()\nprint(json.dumps({"type":"thread.started","thread_id":"extraction-session"}))\nprint(json.dumps({"type":"result","structured_output":{"passed":True,"issues":[]}}))')
    seen = []

    def cli(agent, session, schema, model):
        seen.append(session)
        return [sys.executable, str(script)]

    monkeypatch.setattr('transmux.agents.command', cli)
    runner = AgentRunner(store)
    assert (await runner.run_isolated(pid, jid, 'extract', REVIEW))['passed']
    assert (await runner.run_isolated(pid, jid, 'summarize', REVIEW))['passed']
    assert seen == [None, None]
    assert store.project(pid)['session'] == 'chat-session'


async def test_codebuddy_assistant_context_error_stops_without_waiting_for_result(tmp_path, monkeypatch):
    store = Store(tmp_path / 'data')
    pid = store.create_project('Context error', 'codebuddy')['id']
    jid = store.enqueue(pid, 'style', {})['id']
    (store.workspace(pid) / 'runs' / jid).mkdir()
    script = tmp_path / 'error.py'
    script.write_text('import sys,json,time\nsys.stdin.read()\nprint(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"400 prompt is too long: 100001 tokens > 100000 maximum (request/session)"}]}}),flush=True)\ntime.sleep(30)')
    monkeypatch.setattr('transmux.agents.command', lambda *args: [sys.executable, str(script)])
    from transmux.agents import ContextLimitError
    with pytest.raises(ContextLimitError):
        await asyncio.wait_for(AgentRunner(store).run_isolated(pid, jid, 'extract', REVIEW), timeout=5)
    store.db.close()
