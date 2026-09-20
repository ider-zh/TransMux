import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from transmux.app import create_app
from transmux.jobs import Worker
from transmux.languages import paragraph_language
from transmux.rag import Rag
from transmux.store import Store
from test_workflows import FakeRunner, TinyEmbeddings, add_source, job


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path)
    yield instance
    instance.db.close()


def corpus(store, pid, paragraphs):
    path = store.workspace(pid) / 'corpus' / 'mixed.txt'
    path.write_text('\n'.join(paragraphs))
    Path(str(path) + '.json').write_text(json.dumps(paragraphs, ensure_ascii=False))
    return store.add_file(pid, 'mixed.txt', 'corpus', path)


ENGLISH = 'The doctor works in a hospital and provides medical care to patients.'
CHINESE = '医生在医院工作，为患者提供专业的医疗服务。'


def test_language_detection_is_conservative():
    assert paragraph_language(ENGLISH) == 'en'
    assert paragraph_language(CHINESE) == 'zh-CN'
    for text in ('12345', 'ABC', 'Bonjour le monde, ceci est un document rédigé en français.', 'これは日本語の文書です。'):
        assert paragraph_language(text) == 'unknown'


@pytest.mark.parametrize('language,expected', [('en', ENGLISH), ('zh-CN', CHINESE)])
def test_index_filters_before_retrieval_and_keeps_source_positions(store, language, expected):
    pid = store.create_project('P', 'codex', target_language=language)['id']
    corpus(store, pid, [ENGLISH, CHINESE, '123'])
    rag = Rag(TinyEmbeddings())
    worker = Worker(store, FakeRunner(), rag)
    result = rag.build(store.workspace(pid), worker.corpus(pid), language)
    assert result['included'] == 1 and result['excluded'] == 2
    hits = rag.search_many(store.workspace(pid), worker.corpus(pid), ['医生'], 10, language)[0]
    assert len(hits) == 1 and hits[0]['text'] == expected
    assert hits[0]['paragraph'] == (1 if language == 'en' else 2)
    assert hits[0]['language'] == language
    wrong_language = 'zh-CN' if language == 'en' else 'en'
    with pytest.raises(ValueError, match='重新构建'):
        rag.load(store.workspace(pid), worker.corpus(pid), wrong_language)


async def test_style_only_sees_target_paragraphs_and_automatically_indexes(store):
    pid = store.create_project('P', 'codebuddy')['id']
    corpus(store, pid, [ENGLISH, CHINESE])
    runner = FakeRunner()
    rag = Rag(TinyEmbeddings())
    worker = Worker(store, runner, rag)
    queued = store.enqueue(pid, 'style', {})
    await worker.execute(queued)
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'succeeded'
    assert ENGLISH in runner.calls[0][2] and CHINESE not in runner.calls[0][2]
    assert rag.load(store.workspace(pid), worker.corpus(pid))['counts'] == {'included':1, 'excluded':1}


class BrokenEmbeddings(TinyEmbeddings):
    def encode(self, texts):
        raise RuntimeError('embedding service down')


async def test_index_failure_preserves_style_and_pauses_translation(store):
    pid = store.create_project('P', 'codex')['id']
    corpus(store, pid, [ENGLISH])
    worker = Worker(store, FakeRunner(), Rag(BrokenEmbeddings()))
    task = store.enqueue(pid, 'style', {})
    await worker.execute(task)
    result = store.rows('SELECT state,result FROM jobs WHERE id=?', (task['id'],))[0]
    assert result['state'] == 'needs_attention'
    assert '风格与术语已更新' in result['result']
    assert 'Translation Style' in store.snapshot_config(pid, 'style')['content']
    source = add_source(store, pid)
    translation = job(store, pid, source)
    await worker.execute(translation)
    assert store.rows('SELECT state FROM jobs WHERE id=?', (translation['id'],))[0]['state'] == 'needs_attention'
    assert not store.rows("SELECT * FROM files WHERE kind='output'")
    # Turning retrieval off explicitly allows translation without invoking embeddings.
    store.execute('UPDATE projects SET use_rag=0 WHERE id=?', (pid,))
    translation = job(store, pid, source)
    await worker.execute(translation)
    assert store.rows('SELECT state FROM jobs WHERE id=?', (translation['id'],))[0]['state'] == 'succeeded'
    assert 'Translation Style' in worker.runner.calls[-2][2]


async def test_no_target_corpus_translates_with_explicit_fallback(store):
    pid = store.create_project('P', 'codex')['id']
    corpus(store, pid, [CHINESE])
    runner = FakeRunner()
    worker = Worker(store, runner, Rag(BrokenEmbeddings()))
    task = job(store, pid, add_source(store, pid))
    await worker.execute(task)
    assert store.rows('SELECT state FROM jobs')[0]['state'] == 'succeeded'
    assert 'general accurate' in runner.calls[0][2]
    events = store.rows('SELECT * FROM events WHERE job=?', (task['id'],))
    assert any('本次未使用 RAG' in e['text'] for e in events)
    phases = [json.loads(e['text']) for e in events if e['kind'] == 'phase']
    assert any(p['stage'] == 'reviewing' and p['start'] == 1 and p['end'] == 2 for p in phases)
    assert phases[-1]['stage'] == 'exporting' and phases[-1]['completed'] == 2
    progress = json.loads(store.rows('SELECT progress FROM jobs')[0]['progress'])
    assert progress == phases[-1]


def test_fixed_language_api_and_persisted_preference(tmp_path, monkeypatch):
    monkeypatch.setattr('transmux.app.availability', lambda: [{'id':'codex','available':True}])
    with TestClient(create_app(tmp_path)) as client:
        a = client.post('/api/projects',json={'name':'English project','agent':'codex'}).json()
        assert a['target_language'] == 'en' and a['use_rag'] == 1
        b = client.post('/api/projects',json={'name':'Chinese project','agent':'codex','target_language':'zh-CN'}).json()
        assert b['target_language'] == 'zh-CN'
        base = '/api/projects/' + a['id']
        assert client.patch(base,json={'target_language':'zh-CN'}).status_code == 422
        assert client.post(base+'/jobs',json={'kind':'translate','target_language':'zh-CN'}).status_code == 422
        assert client.patch(base+'/rag-preference',json={'use_rag':False}).json()['use_rag'] == 0
        db = sqlite3.connect(tmp_path / 'transmux.sqlite3')
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE projects SET target_language='zh-CN' WHERE id=?", (a['id'],))
        db.close()
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(base).json()['use_rag'] == 0
