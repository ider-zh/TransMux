import json

import pytest
from fastapi.testclient import TestClient

from transmux.agents import model_choices
from transmux.app import create_app
from transmux.model_catalog import live_models, model_catalog


@pytest.mark.parametrize('agent', ['codex', 'codebuddy'])
async def test_live_protocol_and_pagination_without_prompt(tmp_path, monkeypatch, agent):
    cli = tmp_path / 'agent'
    cli.write_text('''#!/usr/bin/env python3
import sys,json
for line in sys.stdin:
 r=json.loads(line)
 if 'method' in r:
  if r['method']=='initialized': continue
  if r['method']=='initialize': result={}
  else:
   assert r['method']=='model/list'
   cursor=r['params'].get('cursor')
   result={'data':[{'model':'second' if cursor else 'first','displayName':'Display','isDefault':not cursor}], 'nextCursor':None if cursor else 'page2'}
  print(json.dumps({'id':r['id'],'result':result}),flush=True)
 else:
  assert r['type']=='control_request'
  if r['request']['subtype']=='initialize':
   assert r['request']['hasPrompt'] is False
   result={}
  else:
   assert r['request']['subtype']=='get_available_models'
   result={'availableModels':[{'modelId':'buddy-model','name':'Buddy'}]}
  print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':r['request_id'],'response':result}}),flush=True)
''')
    cli.chmod(0o700)
    monkeypatch.setenv('TRANSMUX_' + agent.upper() + '_BIN', str(cli))
    result = await live_models(agent)
    assert [m['id'] for m in result] == (['first', 'second'] if agent == 'codex' else ['buddy-model'])


async def test_refresh_falls_back_honestly_and_next_request_retries(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    monkeypatch.delenv('TRANSMUX_CODEX_MODELS', raising=False)
    path = tmp_path / 'models_cache.json'
    path.write_text(json.dumps({'models': [{'slug': 'cached'}]}))
    calls = []
    async def discover(agent):
        calls.append(agent)
        if len(calls) == 1:
            raise TimeoutError()
        return [{'id': 'fresh', 'name': 'Fresh', 'default': True}]
    monkeypatch.setattr('transmux.model_catalog.live_models', discover)
    fallback = await model_catalog('codex')
    assert fallback['source'] == 'cache' and fallback['models'] == ['cached'] and fallback['warning']
    result = await model_catalog('codex')
    assert result['source'] == 'agent' and result['default_model'] == 'fresh'
    path.write_text(json.dumps({'models': [{'slug': 'updated'}]}))
    assert model_choices('codex') == ['updated']


def test_refresh_api_and_initial_model_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id':'codex','available':True}])
    async def discover(agent):
        return [{'id':'fresh-model','name':'Fresh model','default':True}]
    monkeypatch.setattr('transmux.model_catalog.live_models', discover)
    monkeypatch.delenv('TRANSMUX_CODEX_MODELS', raising=False)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/agents/codex/models?refresh=true').json()['models'] == ['fresh-model']
        p = client.post('/api/projects', json={'name':'Initial selection','agent':'codex','model':'fresh-model'}).json()
        assert client.get('/api/projects/' + p['id']).json()['model'] == 'fresh-model'
