import pytest

from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import Store
from test_workflows import FakeRunner, TinyEmbeddings, add_source


async def test_update_status_survives_reload_and_rag_does_not_clear_it(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Corpus', 'codex')['id']
    worker = Worker(store, FakeRunner(), Rag(TinyEmbeddings()))
    assert not store.corpus_style_pending(pid)
    add_source(store, pid, 'corpus')
    assert store.corpus_style_pending(pid)
    await worker.execute(store.enqueue(pid, 'rag', {}))
    assert store.corpus_style_pending(pid)
    await worker.execute(store.enqueue(pid, 'style', {}))
    assert not store.corpus_style_pending(pid)
    store.db.close()
    store = Store(tmp_path)
    assert not store.corpus_style_pending(pid)
    fid = add_source(store, pid, 'corpus')
    assert store.corpus_style_pending(pid)
    worker = Worker(store, FakeRunner(), Rag(TinyEmbeddings()))
    await worker.execute(store.enqueue(pid, 'style', {}))
    assert not store.corpus_style_pending(pid)
    store.delete_corpus(pid, fid)
    assert store.corpus_style_pending(pid)
    other = store.create_project('Other', 'codebuddy')['id']
    assert not store.corpus_style_pending(other)
    store.db.close()


@pytest.mark.parametrize('fail', [False, True])
async def test_failure_or_upload_during_extraction_stays_pending(tmp_path, fail):
    store = Store(tmp_path)
    pid = store.create_project('Corpus', 'codex')['id']
    add_source(store, pid, 'corpus')

    class Runner(FakeRunner):
        async def run(self, *args, **kwargs):
            if fail:
                raise ValueError('Test extraction failure')
            add_source(store, pid, 'corpus')
            return await super().run(*args, **kwargs)

    worker = Worker(store, Runner(), Rag(TinyEmbeddings()))
    await worker.execute(store.enqueue(pid, 'style', {}))
    assert store.corpus_style_pending(pid)
    store.db.close()


async def test_existing_workspace_uses_successful_extraction_history(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project('Legacy corpus', 'codex')['id']
    fid = add_source(store, pid, 'corpus')
    add_source(store, pid, 'corpus')
    worker = Worker(store, FakeRunner(), Rag(TinyEmbeddings()))
    await worker.execute(store.enqueue(pid, 'style', {}))
    (store.workspace(pid) / 'style-corpus.json').unlink()
    assert not store.corpus_style_pending(pid)
    store.delete_corpus(pid, fid)
    assert store.corpus_style_pending(pid)
    store.db.close()
