import json
from pathlib import Path

from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import Store
from transmux.style_pipeline import batches, records_for, sample_indices, tokens
from test_workflows import FakeRunner, TinyEmbeddings


def corpus(store, pid, name='reference', paragraphs=None):
    path = store.workspace(pid) / 'corpus' / (name + '.txt')
    blocks = paragraphs or ['The doctor works in a hospital. The results are explained clearly and precisely.']
    path.write_text('\n'.join(blocks))
    Path(str(path) + '.json').write_text(json.dumps(blocks))
    return store.add_file(pid, path.name, 'corpus', path)


async def run(store, worker, pid):
    task = store.enqueue(pid, 'style', {})
    await worker.execute(task)
    result = store.rows('SELECT * FROM jobs WHERE id=?', (task['id'],))[0]
    return result, store.workspace(pid) / 'runs' / task['id']


def test_many_short_paragraphs_no_longer_create_hundreds_of_batches():
    records = [dict(text='This sentence explains the experimental result clearly.', paragraph=i, section='', body=True)
               for i in range(1600)]
    selected = sample_indices(records)
    for i, r in enumerate(records):
        r['style_sample'] = i in selected
    planned = list(batches(records))
    assert len(planned) < 20  # Old 8-paragraph limit produces 200 calls.
    assert sum(len(b) for b in planned) == 1600
    assert len(selected) == 24 and 0 in selected and 1599 in selected
    assert all(sum(tokens(r) + 30 for r in b) <= 10000 for b in planned)


async def test_cache_append_delete_and_reopen(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Cache', 'codex')['id']
    first = corpus(store, pid)
    runner = FakeRunner()
    worker = Worker(store, runner, Rag(TinyEmbeddings()))
    result, _ = await run(store, worker, pid)
    assert result['state'] == 'succeeded', result['result']
    assert len(runner.calls) == 2
    store.db.close()
    store = Store(tmp_path)
    worker = Worker(store, runner, Rag(TinyEmbeddings()))
    result, path = await run(store, worker, pid)
    assert result['state'] == 'succeeded' and len(runner.calls) == 2
    assert json.loads((path / 'extraction-plan.json').read_text())['cached_batches'] == 1
    corpus(store, pid, 'new', ['The doctor describes another experiment in a concise and professional manner.'])
    result, path = await run(store, worker, pid)
    assert result['state'] == 'succeeded'
    assert sum('observations' in c[3]['properties'] for c in runner.calls) == 2
    store.delete_corpus(pid, first)
    result, path = await run(store, worker, pid)
    assert result['state'] == 'succeeded'
    evidence = json.loads((path / 'style-observations.json').read_text())
    assert all(e['file_id'] != first for r in evidence for e in r['evidence'])
    assert sum('observations' in c[3]['properties'] for c in runner.calls) == 2
    store.db.close()


async def test_failed_later_document_resumes_completed_cache(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Resume', 'codex')['id']
    corpus(store, pid, 'one')
    corpus(store, pid, 'two', ['The doctor describes a different experiment using clear professional English.'])

    class Runner(FakeRunner):
        fail = True
        async def run(self, pid, jid, prompt, schema=None):
            if self.fail and 'different experiment' in prompt:
                raise ValueError('Temporary failure')
            return await super().run(pid, jid, prompt, schema)

    runner = Runner()
    worker = Worker(store, runner, Rag(TinyEmbeddings()))
    before = store.snapshot_config(pid, 'style')
    result, _ = await run(store, worker, pid)
    assert result['state'] == 'failed'
    assert store.snapshot_config(pid, 'style') == before
    runner.fail = False
    result, path = await run(store, worker, pid)
    assert result['state'] == 'succeeded'
    assert json.loads((path / 'extraction-plan.json').read_text())['cached_batches'] == 1
    assert sum('observations' in c[3]['properties'] for c in runner.calls) == 2
    store.db.close()


async def test_rag_failure_does_not_dirty_completed_extraction(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('RAG failure', 'codex')['id']
    corpus(store, pid)

    class BrokenRag(Rag):
        def build(self, *args):
            raise ValueError('Index unavailable')

    worker = Worker(store, FakeRunner(), BrokenRag(TinyEmbeddings()))
    result, _ = await run(store, worker, pid)
    assert result['state'] == 'needs_attention'
    assert not store.corpus_style_pending(pid)
    assert '仅需重试参考索引' in result['result']
    store.db.close()


async def test_atomic_style_save_rolls_back_terms_and_marker(tmp_path, monkeypatch):
    store = Store(tmp_path)
    pid = store.create_project('Atomic', 'codex')['id']
    corpus(store, pid)
    before = {n: store.snapshot_config(pid, n) for n in ('style', 'terms', 'people')}
    import transmux.store as store_module
    write = store_module.atomic_write
    failed = False

    def fail_once(path, content):
        nonlocal failed
        if path.name == 'people.json' and not failed:
            failed = True
            raise OSError('Injected write failure')
        return write(path, content)

    monkeypatch.setattr(store_module, 'atomic_write', fail_once)
    result, _ = await run(store, Worker(store, FakeRunner(), Rag(TinyEmbeddings())), pid)
    assert result['state'] == 'failed'
    assert before == {n: store.snapshot_config(pid, n) for n in before}
    assert not (store.workspace(pid) / 'style-corpus.json').exists()
    store.db.close()


async def test_user_requirements_preserved_and_edit_conflicts_block_commit(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Manual', 'codex')['id']
    corpus(store, pid)
    style = store.snapshot_config(pid, 'style')
    store.write_config(pid, 'style', 'Always use British English spelling.', style['revision'])
    required = store.snapshot_config(pid, 'requirements')
    runner = FakeRunner()
    result, _ = await run(store, Worker(store, runner, Rag(TinyEmbeddings())), pid)
    assert result['state'] == 'succeeded'
    assert store.snapshot_config(pid, 'requirements') == required
    assert 'British English' in runner.calls[-1][2]
    # No requirements in cached corpus observations; only the final synthesis depends on them.
    assert 'British English' not in runner.calls[0][2]
    store.write_config(pid, 'requirements', 'Prefer concise sentences.', required['revision'])

    class Editor(FakeRunner):
        async def run(self, *args):
            current = store.snapshot_config(pid, 'requirements')
            store.write_config(pid, 'requirements', 'Retain all source details.', current['revision'])
            return await super().run(*args)

    before = store.snapshot_config(pid, 'style')
    result, _ = await run(store, Worker(store, Editor(), Rag(TinyEmbeddings())), pid)
    assert result['state'] == 'needs_attention'
    assert store.snapshot_config(pid, 'style') == before
    store.db.close()


def test_content_changes_invalidate_extraction_input(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Content', 'codex')['id']
    fid = corpus(store, pid)
    file = store.file(pid, fid)
    before, _ = records_for(store.workspace(pid), file, 'en')
    Path(str(store.workspace(pid) / file['path']) + '.json').write_text(json.dumps(['The doctor explains a revised experiment in plain English.']))
    after, _ = records_for(store.workspace(pid), file, 'en')
    assert before != after
    store.db.close()


async def test_requirements_api_history_and_language(tmp_path):
    import httpx
    from transmux.app import create_app
    app = create_app(tmp_path, lambda store: Worker(store, FakeRunner(), Rag(TinyEmbeddings())))
    async with app.router.lifespan_context(app):
        pid = app.state.store.create_project('Requirements API', 'codex')['id']
        base = f'/api/projects/{pid}/config/'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            current = (await client.get(base + 'requirements')).json()
            assert current['content'] == ''
            updated = await client.put(base + 'requirements', json={'content': 'Prefer British spelling.', 'revision': current['revision']})
            assert updated.status_code == 200
            rejected = await client.put(base + 'requirements', json={'content': '这里使用中文要求。', 'revision': updated.json()['revision']})
            assert rejected.status_code == 400
            history = (await client.get(base + 'requirements/history')).json()
            assert history[0]['content'] == 'Prefer British spelling.'
            style = (await client.get(base + 'style')).json()
            response = await client.put(base + 'style', json={'content': 'Preserve every factual detail.', 'revision': style['revision']})
            assert response.status_code == 200
            assert (await client.get(base + 'requirements')).json()['content'] == 'Preserve every factual detail.'


def test_migration_preserves_manual_requirements_before_automatic_updates(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Legacy manual', 'codex')['id']
    current = store.snapshot_config(pid, 'style')
    store.write_config(pid, 'style', 'Use British English spelling.', current['revision'])
    current = store.snapshot_config(pid, 'style')
    store.write_config(pid, 'style', 'Use a concise professional style.', current['revision'], 'extraction', 'old-task')
    (store.workspace(pid) / 'requirements.md').unlink()
    store.ensure_style_requirements(pid)
    assert store.snapshot_config(pid, 'requirements')['content'] == 'Use British English spelling.'
    store.db.close()


async def test_unsupported_observation_does_not_publish(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Evidence', 'codex')['id']
    corpus(store, pid)

    class InventedQuote(FakeRunner):
        async def run(self, *args):
            response = await super().run(*args)
            response['observations'][0]['quote'] = 'This sentence does not exist in the supplied reference.'
            return response

    before = {n: store.snapshot_config(pid, n) for n in ('style', 'terms', 'people')}
    result, _ = await run(store, Worker(store, InventedQuote(), Rag(TinyEmbeddings())), pid)
    assert result['state'] == 'needs_attention'
    assert before == {n: store.snapshot_config(pid, n) for n in before}
    store.db.close()


def test_long_chinese_block_respects_budget_and_keeps_all_text(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Chinese budget', 'codex', target_language='zh-CN')['id']
    text = '本文研究语言理解的方法，并介绍实验结果。' * 1000
    fid = corpus(store, pid, paragraphs=[text])
    records, _ = records_for(store.workspace(pid), store.file(pid, fid), 'zh-CN')
    assert ''.join(r['text'] for r in records) == text
    for r in records:
        r['style_sample'] = True
    assert all(sum(tokens(r) + 30 for r in batch) <= 10000 for batch in batches(records))
    assert all(r['paragraph'] == 1 for r in records)
    store.db.close()


def test_style_quote_typography_preserves_original_evidence():
    from transmux.style_pipeline import observations
    text = 'The author calls it “baffling” and explains\n  the result clearly.'
    batch = [{'text': text, 'paragraph': 42, 'style_sample': True}]
    response = {'observations': [{'rule': 'Use clear explanatory prose.', 'paragraph': 1,
                                 'quote': 'calls it "baffling" and explains the result'}]}
    result = observations(response, batch, 'en')
    assert result[0]['evidence'] == [{'paragraph': 42, 'quote': 'calls it “baffling” and explains\n  the result'}]


def test_style_quote_rejects_paraphrase_wrong_paragraph_and_non_sample():
    import pytest
    from transmux.style_pipeline import observations
    batch = [{'text': 'The result is not supported.', 'paragraph': 1, 'style_sample': True},
             {'text': 'The result is supported.', 'paragraph': 2, 'style_sample': False}]
    for quote, number in [('The result is supported.', 1), ('The result is supported.', 2),
                          ('THE RESULT IS NOT SUPPORTED.', 1)]:
        with pytest.raises(ValueError, match='指定样本'):
            observations({'observations': [{'rule': 'Use clear explanatory prose.', 'paragraph': number, 'quote': quote}]}, batch, 'en')


async def test_style_evidence_repairs_only_failed_batch_and_reuses_cache(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Repair evidence', 'codex')['id']
    corpus(store, pid, 'one')
    corpus(store, pid, 'two', ['The doctor explains a different experiment in clear professional English.'])

    class RepairRunner(FakeRunner):
        broken = False

        async def run(self, *args):
            response = await super().run(*args)
            if 'observations' in response and 'different experiment' in args[2] and not self.broken:
                self.broken = True
                response['observations'][0]['quote'] = 'Invented evidence that does not occur in the source.'
            return response

    runner = RepairRunner()
    worker = Worker(store, runner, Rag(TinyEmbeddings()))
    result, path = await run(store, worker, pid)
    assert result['state'] == 'succeeded', result['result']
    assert sum('observations' in c[3]['properties'] for c in runner.calls) == 3
    assert list(path.glob('style-batch-*-evidence-validation.json'))
    count = len(runner.calls)
    result, path = await run(store, worker, pid)
    assert result['state'] == 'succeeded'
    assert len(runner.calls) == count
    assert json.loads((path / 'extraction-plan.json').read_text())['cached_batches'] == 2
    store.db.close()
