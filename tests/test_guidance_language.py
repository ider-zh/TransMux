import json

import pytest
from fastapi.testclient import TestClient

from transmux.app import create_app
from transmux.jobs import Worker
from transmux.languages import guidance_language_error
from transmux.rag import Rag
from transmux.store import Store
from test_workflows import FakeRunner, TinyEmbeddings, add_source


@pytest.mark.parametrize('target,text,valid', [
    ('en', '# Translation Style\nUse clear, natural English and preserve the original meaning.', True),
    ('en', '# 翻译风格\n语气专业清晰，保留原文段落结构。', False),
    ('en', 'Use a formal register. Render “忠实原意” naturally.\n> 原文示例：这是一个例子。', True),
    ('en', 'Retain the names 李白 and 鲁迅 when appropriate.', True),
    ('en', 'For example, use `术语` as a quoted source term.', True),
    ('en', '# Translation Style\n语气专业清晰，保留原文段落结构。', False),
    ('en', 'Respectez le sens original et utilisez des phrases claires et naturelles.', False),
    ('en', '명확하고 자연스러운 문장을 사용하세요.', False),
    ('zh-CN', '# 翻译风格\n使用自然简洁的中文；必要时保留 HTTP 和 API 等缩写。', True),
    ('zh-CN', '使用准确的中文表达，例如将 “source text” 作为原文引用。', True),
    ('zh-CN', '# Translation Style\nUse clear, professional English.', False),
    ('zh-CN', 'Use clear and professional language and preserve the structure. 中文', False),
    ('en', '“只有引用，没有规则正文。”', False),
])
def test_guidance_language_checks_prose_but_allows_foreign_examples(target, text, valid):
    assert (guidance_language_error(text, target, required=True) is None) is valid


def test_new_projects_initialize_target_language_and_save_checks(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id':'codex', 'available':True}])
    with TestClient(create_app(tmp_path)) as client:
        english = client.post('/api/projects', json=dict(name='English', agent='codex')).json()['id']
        chinese = client.post('/api/projects', json=dict(name='中文', agent='codex', target_language='zh-CN')).json()['id']
        base = f'/api/projects/{english}/config/'
        current = client.get(base + 'style').json()
        assert '# Translation Style' in current['content']
        assert '# 翻译风格' in client.get(f'/api/projects/{chinese}/config/style').json()['content']
        response = client.put(base + 'style', json=dict(content='# 翻译风格\n保留原文结构，忠实原意。', revision=current['revision']))
        assert response.status_code == 400
        assert client.get(base + 'style').json() == current
        terms = client.get(base + 'terms').json()
        data = json.loads(terms['content'])
        data['rows'] = [dict(term='doctor', meaning='医疗从业人员', usage='Use in a medical context.', source='', origin='manual')]
        assert client.put(base + 'terms', json=dict(content=json.dumps(data), revision=terms['revision'])).status_code == 400
        data['rows'][0]['meaning'] = 'A medical professional.'
        assert client.put(base + 'terms', json=dict(content=json.dumps(data), revision=terms['revision'])).status_code == 200
        pairs = client.get(base + 'mappings').json()
        data = json.loads(pairs['content'])
        data['rows'] = [dict(original='医生', translation='doctor', context='Medicine', source='', origin='manual')]
        assert client.put(base + 'mappings', json=dict(content=json.dumps(data), revision=pairs['revision'])).status_code == 200


async def test_wrong_language_style_preserves_all_configs_and_retains_candidate(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex')['id']
    add_source(store, pid, 'corpus')
    before = {name: store.snapshot_config(pid, name) for name in ('style', 'terms', 'mappings')}

    class ChineseStyle(FakeRunner):
        async def run(self, *args):
            response = await super().run(*args)
            if response.get('observations'):
                response['observations'][0]['rule'] = '语气专业、清晰，保留原文结构。'
            return response

    runner = ChineseStyle()
    task = store.enqueue(pid, 'style', {})
    await Worker(store, runner, Rag(TinyEmbeddings())).execute(task)
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'needs_attention'
    assert len(runner.calls) == 1
    assert before == {name: store.snapshot_config(pid, name) for name in before}
    assert (store.workspace(pid) / 'runs' / task['id'] / 'style-batch-1-response.json').exists()
    store.db.close()


async def test_legacy_chinese_style_is_rewritten_and_wrong_language_terms_are_skipped(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex')['id']
    add_source(store, pid, 'corpus')
    (store.workspace(pid) / 'style.md').write_text('# 翻译风格\n保留原文结构，忠实原意。')

    class MixedTerms(FakeRunner):
        async def run(self, *args):
            response = await super().run(*args)
            if 'terms' in response:
                response['terms'].append(dict(term='hospital', meaning='医院', usage='医疗领域使用', paragraph=1))
            return response

    runner = MixedTerms()
    task = store.enqueue(pid, 'style', {})
    await Worker(store, runner, Rag(TinyEmbeddings())).execute(task)
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'succeeded'
    assert '# Translation Style' in store.snapshot_config(pid, 'style')['content']
    terms = json.loads(store.snapshot_config(pid, 'terms')['content'])['rows']
    assert len(terms) == 1 and terms[0]['term'] == 'doctor'
    prompt = runner.calls[0][2]
    assert 'headings and prose use the target language' in prompt
    assert 'Do not write a complete style guide' in prompt
    assert '仅依据' not in prompt
    report = json.loads((store.workspace(pid) / 'runs' / task['id'] / 'style-batch-1-validation.json').read_text())
    assert report[0]['status'] == 'skipped' and 'meaning' in report[0]['reason']
    store.db.close()


async def test_chat_style_changes_go_through_language_validation(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex')['id']
    original = store.snapshot_config(pid, 'style')

    class ChatRunner:
        async def run(self, pid, jid, prompt, schema=None):
            assert 'This is a chat task' in prompt
            (store.workspace(pid) / 'runs' / jid / 'proposed-style.md').write_text('# 翻译风格\n使用中文说明翻译规范。')
            return 'Proposed changes.'

    await Worker(store, ChatRunner()).execute(store.enqueue(pid, 'chat', {'message':'调整风格'}))
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'failed'
    assert store.snapshot_config(pid, 'style') == original
    store.db.close()


@pytest.mark.parametrize('target', ['en', 'zh-CN'])
async def test_target_language_extraction_succeeds_with_english_instructions(tmp_path, target):
    from test_language_pipeline import corpus, ENGLISH, CHINESE
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex', target_language=target)['id']
    corpus(store, pid, [ENGLISH, CHINESE])

    class TargetRunner(FakeRunner):
        async def run(self, pid, jid, prompt, schema=None):
            assert prompt.startswith('You are a professional document translation agent.')
            assert '仅依据' not in prompt
            assert ('fixed target language is English' if target == 'en' else 'fixed target language is Simplified Chinese') in prompt
            if target == 'en':
                return await super().run(pid, jid, prompt, schema)
            if 'rules' in schema['properties']:
                return {'rules': [dict(text='使用准确、清晰、自然的简体中文。', evidence_ids=[1])]}
            paragraphs = json.loads(prompt.split('\n')[-1])['paragraphs']
            sample = next(r for r in paragraphs if r['style_sample'])
            return dict(observations=[dict(rule='使用准确、清晰、自然的简体中文。', paragraph=sample['paragraph'], quote=sample['text'])], terms=[
                dict(term='医生', meaning='提供医疗服务的专业人员', usage='在医疗语境中使用', paragraph=1, need='preferred_variant', reason='本项目统一使用医生，避免与医师混用', scope='医疗服务')], people=[])

    task = store.enqueue(pid, 'style', {})
    await Worker(store, TargetRunner(), Rag(TinyEmbeddings())).execute(task)
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'succeeded'
    terms = json.loads(store.snapshot_config(pid, 'terms')['content'])['rows']
    assert len(terms) == 1
    assert guidance_language_error(store.snapshot_config(pid, 'style')['content'], target) is None
    assert guidance_language_error(terms[0]['meaning'], target) is None
    store.db.close()


async def test_mapping_context_language_is_skipped_without_blocking_translation(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('test', 'codex')['id']
    fid = add_source(store, pid)

    class MappingRunner(FakeRunner):
        async def run(self, *args):
            response = await super().run(*args)
            if 'translations' in response:
                response['translations'] = ['The doctor works in a hospital.', 'Keep the second paragraph.']
                response['mappings'] = [dict(original='doctor', translation='doctor', context='医疗领域', paragraph=1)]
            return response

    runner = MappingRunner()
    task = store.enqueue(pid, 'translate', dict(file_id=fid, max_review_rounds=1, use_rag=False))
    await Worker(store, runner, Rag(TinyEmbeddings())).execute(task)
    assert len(runner.calls) == 2
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'succeeded'
    review = json.loads((store.workspace(pid) / 'runs' / task['id'] / 'batch-1-review-1.json').read_text())
    assert review['passed'] is True and review['issues'] == []
    report = json.loads((store.workspace(pid) / 'runs' / task['id'] / 'batch-1-mapping-validation-1.json').read_text())
    assert report[0]['status'] == 'skipped' and 'context' in report[0]['reason']
    assert json.loads(store.snapshot_config(pid, 'mappings')['content'])['rows'] == []
    assert store.rows("SELECT * FROM files WHERE kind='output'")
    store.db.close()
