import io
import json

from fastapi.testclient import TestClient
from openpyxl import Workbook
import pytest

from transmux import terminology as terms, term_import, term_policy, term_review
from transmux.app import create_app
from transmux.store import Store, ConfigConflict


def term(**updates):
    return dict(term='Script', meaning='A concept', usage='Use the preferred form', source='paper.txt',
                origin='extraction', scope='Cognitive theories', need='ambiguous', reason='Disambiguate from executable code', **updates)


def empty():
    return dict(rows=[], deleted=[], legacy='')


def test_admission_is_about_consistency_not_word_length():
    definitions = [dict(term='Script', meaning='A representational concept', usage='', scope='', need='none', reason='A definition'),
                   dict(term='data', meaning='Information', usage='', scope='', reason='Common noun')]
    accepted, skipped = term_policy.screen(definitions + [term()], 'en')
    assert [r['term'] for r in accepted] == ['Script']
    assert len(skipped) == 2


def test_scopes_coexist_and_conflicts_do_not_override_manual_or_disabled_rules():
    first = term()
    first['origin'] = 'manual'
    doc = dict(rows=[first], deleted=[], legacy='')
    alternative = {**first, 'meaning': 'An executable program', 'usage': 'Use as code'}
    new, _ = terms.merge('terms', doc, [alternative], 'extraction')
    assert new['rows'][0] == first
    assert new['rows'][1]['status'] == 'pending'
    assert sum(terms.active(r) for r in new['rows']) == 1
    disabled = terms.resolve('terms', new, terms.key('terms', new['rows'][1]), 'deactivate')
    repeated, _ = terms.merge('terms', disabled, [alternative], 'extraction')
    assert repeated == disabled
    separate, _ = terms.merge('terms', doc, [{**alternative, 'scope': 'Programming'}], 'extraction')
    assert len(separate['rows']) == 2
    activated = terms.resolve('terms', new, terms.key('terms', new['rows'][1]), 'activate')
    assert len(activated['rows']) == 1 and activated['rows'][0]['meaning'] == 'An executable program'


def test_people_uncertain_identity_and_aliases_are_not_guessed():
    source = ['Minsky discussed the idea. 王海青 conducted the experiment.']
    candidates = [dict(original='Minsky', translation='Marvin Minsky', aliases='Marvin; Minsky', context='AI research', reason='Possible identity', identity_confirmed=True, paragraph=1),
                  dict(original='王海青', translation='', aliases='Wang Haiqing', context='This experiment', reason='Identity unknown', identity_confirmed=False, paragraph=1)]
    accepted, report = term_policy.names(candidates, source, 'en', [])
    assert not report and all(r['status'] == 'pending' and not r['aliases'] for r in accepted)
    assert all(not terms.active(r) for r in accepted)
    verified, _ = term_policy.names([{**candidates[0], 'translation': 'Minsky', 'aliases': 'Invented'}], source, 'en', [])
    assert verified[0]['status'] == 'active' and verified[0]['aliases'] == ''


def test_csv_xlsx_parse_and_formula_refusal():
    parsed = term_import.parse_table('原文,译文,语境\n马文,Marvin,AI\n'.encode(), 'names.csv')
    assert parsed['cells'][1][0] == '马文'
    book = Workbook()
    sheet = book.active
    sheet.append(['original', 'translation'])
    sheet.append(['Minsky', 'Minsky'])
    stream = io.BytesIO()
    book.save(stream)
    assert term_import.parse_table(stream.getvalue(), 'names.xlsx')['cells'][1] == ['Minsky', 'Minsky']
    sheet['A3'] = '=1+1'
    stream = io.BytesIO()
    book.save(stream)
    with pytest.raises(ValueError, match='公式'):
        term_import.parse_table(stream.getvalue(), 'names.xlsx')


def test_import_requires_explicit_conflict_resolution_and_keeps_unknown_names_pending():
    initial = term_import.apply_import('people', empty(), [dict(action='add', row=dict(original='A',translation='Alice',context='Researcher'))])
    preview = term_import.preview('people', initial, [dict(original='A',translation='Alice',context='Researcher'), dict(original='A',translation='Alex',context='Researcher'), dict(original='王海青')])
    assert [r['action'] for r in preview] == ['duplicate', 'conflict', 'add']
    assert preview[-1]['row']['status'] == 'pending'
    with pytest.raises(ValueError, match='冲突'):
        term_import.apply_import('people', initial, [dict(action='add',row=preview[1]['row'])])
    updated = term_import.apply_import('people', initial, [dict(action='replace', row=preview[1]['row'])])
    assert updated['rows'][0]['translation'] == 'Alex'
    assert initial['rows'][0]['translation'] == 'Alice'


def test_import_api_revision_isolation_and_people_history(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        from unittest.mock import patch
        with patch('transmux.app.availability', return_value=[{'id':'codex','available':True}]):
            pid = client.post('/api/projects', json=dict(name='test', agent='codex')).json()['id']
            other = client.post('/api/projects', json=dict(name='other', agent='codex')).json()['id']
        base = f'/api/projects/{pid}'
        before = client.get(base + '/config/people').json()
        preview = client.post(base + '/terminology/people/preview', json=dict(rows=[dict(original='王海青')])).json()
        assert client.get(base + '/config/people').json() == before
        payload = dict(revision=preview['revision'], operations=[dict(action='add',row=preview['items'][0]['row'])])
        response = client.post(base + '/terminology/people/import', json=payload)
        assert response.status_code == 200
        assert json.loads(response.json()['content'])['rows'][0]['status'] == 'pending'
        assert client.post(base + '/terminology/people/import', json=payload).status_code in (400,409)
        assert len(client.get(base + '/config/people/history').json()) == 2
        assert json.loads(client.get(f'/api/projects/{other}/config/people').json()['content'])['rows'] == []
        assert client.get(base + '/terminology-review/nonexistent').status_code == 400


def test_screening_bundle_rejects_stale_revision_and_keeps_manual_entries(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('review', 'codex')['id']
    store.merge_terminology(pid, 'terms', [term()], 'extraction', 'test')
    snapshots = {k: store.snapshot_config(pid,k) for k in terms.KINDS}
    docs = {k: terms.decode(k,s['content']) for k,s in snapshots.items()}
    old = docs['terms']['rows'][0]
    proposal = dict(items=[dict(index=0,kind='terms',row_index=0,action='disable',proposed={**old,'status':'inactive'})])
    updated = term_review.apply_proposal(docs, proposal, [dict(index=0,edits={})])
    assert terms.active(docs['terms']['rows'][0])
    store.write_terminology_bundle(pid, updated, {k:s['revision'] for k,s in snapshots.items()})
    assert not terms.active(terms.decode('terms', store.snapshot_config(pid,'terms')['content'])['rows'][0])
    with pytest.raises(ConfigConflict):
        store.write_terminology_bundle(pid, updated, {k:s['revision'] for k,s in snapshots.items()})
    docs['terms']['rows'][0]['origin'] = 'manual'
    with pytest.raises(ValueError, match='用户指定'):
        term_review.apply_proposal(docs, proposal, [dict(index=0,edits={})])
    store.db.close()


async def test_pending_and_inactive_are_excluded_from_agent_snapshot(tmp_path):
    from transmux.jobs import Worker
    store = Store(tmp_path)
    pid = store.create_project('snapshot', 'codex')['id']
    store.merge_terminology(pid, 'terms', [dict(term='UniqueInactiveSentinel',meaning='',usage='',source='',status='inactive'),
                                           dict(term='UniquePendingSentinel',meaning='',usage='',source='',status='pending')], 'extraction','test')

    class Runner:
        async def run(self, pid, jid, prompt, schema=None):
            assert 'UniqueInactiveSentinel' not in prompt and 'UniquePendingSentinel' not in prompt
            return 'Done'

    task = store.enqueue(pid,'chat',dict(message='Explain the style'))
    await Worker(store,Runner()).execute(task)
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'succeeded'
    store.db.close()


def test_bundle_write_failure_rolls_back_files_and_history(tmp_path, monkeypatch):
    import transmux.store as store_module
    store = Store(tmp_path)
    pid = store.create_project('atomic review', 'codex')['id']
    before = {kind: store.snapshot_config(pid, kind) for kind in terms.KINDS}
    documents = {kind: terms.decode(kind, snapshot['content']) for kind, snapshot in before.items()}
    documents['terms']['rows'] = [term()]
    history = store.rows('SELECT * FROM config_history')
    original_write = store_module.atomic_write
    failed = False

    def fail_once(path, content):
        nonlocal failed
        if path.name == 'mappings.json' and not failed:
            failed = True
            raise OSError('Simulated disk write failure')
        return original_write(path, content)

    monkeypatch.setattr(store_module, 'atomic_write', fail_once)
    with pytest.raises(OSError, match='disk'):
        store.write_terminology_bundle(pid, documents, {kind: s['revision'] for kind, s in before.items()})
    assert {kind: store.snapshot_config(pid, kind) for kind in terms.KINDS} == before
    assert store.rows('SELECT * FROM config_history') == history
    store.db.close()


async def test_name_metadata_rejection_does_not_block_correct_text(tmp_path):
    from transmux.jobs import Worker
    from test_workflows import add_source, FakeRunner, job
    store = Store(tmp_path)
    pid = store.create_project('name review', 'codex', target_language='zh-CN')['id']

    class Runner(FakeRunner):
        async def run(self, pid, jid, prompt, schema=None):
            response = await super().run(pid, jid, prompt, schema)
            if 'translations' in response:
                response['people'] = [dict(original='doctor',translation='',aliases='',context='医疗场景',reason='疑似人名，尚未确认',
                                            identity_confirmed=False, source_id='p000001',target_id='p000001:t1')]
            else:
                assert 'people_issues' in schema['properties']
                response['people_issues'] = [dict(person=1, reason='This is a profession, not a person name.')]
            return response

    task = job(store, pid, add_source(store, pid))
    runner = Runner()
    await Worker(store, runner).execute(task)
    result = store.rows('SELECT state FROM jobs WHERE id=?', (task['id'],))[0]
    assert result['state'] == 'succeeded' and len(runner.calls) == 2
    assert terms.decode('people', store.snapshot_config(pid,'people')['content'])['rows'] == []
    store.db.close()
