import json

import pytest

from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import Store
from test_workflows import FakeRunner, TinyEmbeddings, add_source, job


def test_dictionary_forms_are_skipped_without_rewriting_the_translation():
    source = ['理性主义与经验主义。']
    draft = ['The positions are rationalist and empiricist.']
    rows = [dict(original=original, translation=translation, context='Philosophy', paragraph=1)
            for original, translation in [('理性主义', 'rationalism'), ('经验主义', 'empiricism'),
                                          ('理性主义', 'rationalist')]]
    accepted, report = Worker.filter_mappings(rows, source, draft)
    assert accepted == [rows[2]]
    assert len(report) == 2
    assert all('译文' in item['reason'] for item in report)
    assert draft == ['The positions are rationalist and empiricist.']


def test_malformed_optional_mappings_are_reported():
    for rows in (None, 'bad array', [None, {}, {'original': 'x'}]):
        accepted, report = Worker.filter_mappings(rows, ['original'], ['translation'])
        assert accepted == [] and report


def test_mapping_paragraph_indices_are_corrected_only_with_joint_unique_evidence():
    # A short heading was skipped in the Agent's paragraph count in the reported incident.
    source = ['概述', '背景', '自然语言处理建立在第一性原理上。', '归纳推理与神经网络。']
    draft = ['Overview', 'Background', 'Natural language processing uses first principles.', 'Inductive reasoning and neural networks.']
    rows = [dict(original=a, translation=b, context='Artificial intelligence', paragraph=n)
            for a,b,n in [('自然语言处理','natural language processing',2), ('第一性原理','first principles',2),
                          ('归纳推理','inductive reasoning',3), ('神经网络','neural networks',3)]]
    before = json.dumps(rows)
    accepted, report = Worker.filter_mappings(rows, source, draft, 'en')
    assert [r['paragraph'] for r in accepted] == [3,3,4,4]
    assert all(r['status'] == 'corrected' for r in report) and len(report) == 4
    assert json.dumps(rows) == before  # Raw evidence stays intact in the task record.


def test_mapping_ambiguity_and_cross_paragraph_evidence_are_not_guessed():
    row = dict(original='术语', translation='term', context='Language', paragraph=3)
    accepted, report = Worker.filter_mappings([row], ['术语','术语','标题'], ['term','term','Title'])
    assert not accepted and '多个段落' in report[0]['reason']
    accepted, report = Worker.filter_mappings([{**row,'paragraph':2}], ['术语','术语'], ['term','term'])
    assert accepted[0]['paragraph'] == 2 and not report
    accepted, report = Worker.filter_mappings([row], ['术语','标题'], ['Different text','term'])
    assert not accepted and report[0]['status'] == 'skipped'


@pytest.mark.parametrize('issues', [None, 'bad', [{}], [{'mapping':True,'reason':'bad'}],
                                  [{'mapping':2,'reason':'out of range'}], [{'mapping':1,'reason':''}]])
def test_invalid_mapping_review_does_not_publish_unverified_pairs(issues):
    row = dict(original='doctor', translation='医生', context='医学', paragraph=1)
    accepted, report = Worker.reviewed_mappings([row], issues)
    assert accepted == [] and len(report) == 1 and report[0]['candidate'] == row


@pytest.mark.parametrize('text_passed', [True, False])
async def test_optional_mapping_review_is_separate_from_translation_gate(tmp_path, text_passed):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex', target_language='zh-CN')['id']

    class ReviewRunner(FakeRunner):
        async def run(self, pid, jid, prompt, schema=None):
            response = await super().run(pid, jid, prompt, schema)
            if 'translations' in response:
                response['mappings'] = [dict(original='doctor',translation='医生',context='医学',paragraph=1, need='preferred_variant', reason='项目需统一首选译法，避免同一职务或机构名称混用'),
                                        dict(original='hospital',translation='医院',context='数学',paragraph=1, need='preferred_variant', reason='项目需统一首选译法，避免同一职务或机构名称混用')]
            else:
                assert 'mapping_issues' in schema['properties']
                response = {'passed':text_passed, 'issues':[] if text_passed else ['The actual translation omits a sentence.'],
                            'mapping_issues':[{'mapping':2, 'reason':'The domain is medicine, not mathematics.'}]}
            return response

    runner = ReviewRunner()
    task = job(store, pid, add_source(store, pid), rounds=2)
    try:
        await Worker(store, runner, Rag(TinyEmbeddings())).execute(task)
        assert store.rows('SELECT state FROM jobs')[0]['state'] == ('succeeded' if text_passed else 'needs_attention')
        assert len(runner.calls) == (2 if text_passed else 4)
        mappings = json.loads(store.snapshot_config(pid,'mappings')['content'])['rows']
        assert [r['original'] for r in mappings] == (['doctor'] if text_passed else [])
        assert bool(store.rows("SELECT id FROM files WHERE kind='output'")) is text_passed
        run = store.workspace(pid)/'runs'/task['id']
        report = json.loads((run/'batch-1-mapping-review-1.json').read_text())
        assert report[0]['candidate']['original'] == 'hospital'
        if not text_passed:
            assert 'The actual translation omits a sentence.' in runner.calls[2][2]
            assert 'The domain is medicine' not in runner.calls[2][2]
    finally:
        store.db.close()


async def test_corrected_mapping_survives_review_and_accumulates_actual_paragraph(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex', target_language='zh-CN')['id']

    class OffsetRunner(FakeRunner):
        async def run(self, pid, jid, prompt, schema=None):
            response = await super().run(pid, jid, prompt, schema)
            if 'translations' in response:
                response['mappings'] = [dict(original='doctor',translation='医生',context='医学',paragraph=2, need='preferred_variant', reason='项目需统一首选译法，避免同一职务或机构名称混用')]
            else:
                assert '"paragraph": 1' in prompt
                response['mapping_issues'] = []
            return response

    runner = OffsetRunner()
    task = job(store, pid, add_source(store, pid), rounds=1)
    try:
        await Worker(store, runner, Rag(TinyEmbeddings())).execute(task)
        assert store.rows('SELECT state FROM jobs')[0]['state'] == 'succeeded'
        assert len(runner.calls) == 2
        rows = json.loads(store.snapshot_config(pid,'mappings')['content'])['rows']
        assert len(rows) == 1 and '段落 1' in rows[0]['source']
        raw = json.loads((store.workspace(pid)/'runs'/task['id']/'batch-1-response-1.json').read_text())
        assert raw['mappings'][0]['paragraph'] == 2
    finally:
        store.db.close()


@pytest.mark.parametrize('passed', [True, False])
async def test_invalid_mapping_continues_to_review_but_publication_still_requires_pass(tmp_path, monkeypatch, passed):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex', target_language='zh-CN')['id']

    # Isolate this translation/review test from the separate comparison publisher.
    def publish(pid, source_file, output, paragraphs, jid):
        assert len(paragraphs) == 2 and output.is_file()
        return store.add_file(pid, output.name, 'output', output)

    monkeypatch.setattr(store, 'publish_translation', publish)

    class MixedRunner(FakeRunner):
        async def run(self, pid, jid, prompt, schema=None):
            response = await super().run(pid, jid, prompt, schema)
            if 'translations' in response:
                response['mappings'] = [
                    dict(original='doctor', translation='医师', context='医学', paragraph=1, need='preferred_variant', reason='项目需统一首选译法，避免同一职务或机构名称混用'),
                    dict(original='hospital', translation='医院', context='医学', paragraph=1, need='preferred_variant', reason='项目需统一首选译法，避免同一职务或机构名称混用'),
                ]
            elif 'passed' in response:
                run = store.workspace(pid) / 'runs' / jid
                assert (run / 'batch-1-response-1.json').exists()
                assert (run / 'batch-1-draft-1.json').exists()
                assert '"translation": "医师"' not in prompt
                assert '"translation": "医院"' in prompt
            return response

    runner = MixedRunner(passed)
    task = job(store, pid, add_source(store, pid), rounds=1)
    try:
        await Worker(store, runner, Rag(TinyEmbeddings())).execute(task)
        result = store.rows('SELECT state FROM jobs')[0]['state']
        assert result == ('succeeded' if passed else 'needs_attention')
        assert len(runner.calls) == 2
        rows = json.loads(store.snapshot_config(pid, 'mappings')['content'])['rows']
        assert len(rows) == (1 if passed else 0)
        if passed:
            assert rows[0]['translation'] == '医院'
        assert bool(store.rows("SELECT id FROM files WHERE kind='output'")) is passed
        run = store.workspace(pid) / 'runs' / task['id']
        report = json.loads((run / 'batch-1-mapping-validation-1.json').read_text())
        assert len(report) == 1 and report[0]['candidate']['translation'] == '医师'
    finally:
        store.db.close()
