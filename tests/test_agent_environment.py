import json
import os
import sys

import pytest

from transmux.agents import AgentRunner, agent_environment
from transmux.jobs import REVIEW
from transmux.store import Store


def test_proxy_is_agent_scoped_dynamic_and_does_not_expand_shell(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    path.write_text('export TRANSMUX_CODEX_HTTP_PROXY="http://192.168.1.230:10808"\n')
    monkeypatch.setenv('TRANSMUX_ENV_FILE', str(path))
    monkeypatch.setenv('http_proxy', 'http://inherited:8000')
    monkeypatch.delenv('TRANSMUX_CODEX_HTTP_PROXY', raising=False)
    monkeypatch.delenv('TRANSMUX_CODEBUDDY_HTTP_PROXY', raising=False)
    assert agent_environment('codex')['http_proxy'] == 'http://192.168.1.230:10808'
    assert agent_environment('codebuddy')['http_proxy'] == 'http://inherited:8000'
    assert os.environ['http_proxy'] == 'http://inherited:8000'
    path.write_text('TRANSMUX_CODEX_HTTP_PROXY=http://${SECRET}:8080\n')
    monkeypatch.setenv('SECRET', 'private')
    assert agent_environment('codex')['http_proxy'] == 'http://${SECRET}:8080'
    monkeypatch.setenv('TRANSMUX_CODEX_HTTP_PROXY', '')
    assert agent_environment('codex')['http_proxy'] == ''


@pytest.mark.parametrize('agent,expected', [('codex', 'http://192.168.1.230:10808'), ('codebuddy', None)])
async def test_cli_process_receives_only_its_proxy(tmp_path, monkeypatch, agent, expected):
    env_file = tmp_path / '.env'
    env_file.write_text('TRANSMUX_CODEX_HTTP_PROXY=http://192.168.1.230:10808\n')
    monkeypatch.setenv('TRANSMUX_ENV_FILE', str(env_file))
    for name in ('http_proxy', 'TRANSMUX_CODEX_HTTP_PROXY', 'TRANSMUX_CODEBUDDY_HTTP_PROXY'):
        monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path / 'data')
    try:
        pid = store.create_project('Proxy', agent)['id']
        jid = store.enqueue(pid, 'chat', {'message': 'test'})['id']
        work = store.workspace(pid)
        (work / 'runs' / jid).mkdir()
        (work / '.env').write_text('TRANSMUX_CODEX_HTTP_PROXY=http://untrusted-workspace:9999\n')
        marker = tmp_path / 'child-env.json'
        script = tmp_path / 'cli.py'
        script.write_text('import os,sys,json\nfrom pathlib import Path\nsys.stdin.read()\n'
                          f'Path({str(marker)!r}).write_text(json.dumps(os.getenv("http_proxy")))\n'
                          'print(json.dumps({"type":"result","structured_output":{"passed":True,"issues":[]}}))\n')
        monkeypatch.setattr('transmux.agents.command', lambda *args: [sys.executable, str(script)])
        assert (await AgentRunner(store).run_isolated(pid, jid, 'test', REVIEW))['passed']
        assert json.loads(marker.read_text()) == expected
    finally:
        store.db.close()
