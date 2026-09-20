import sys
from types import SimpleNamespace

import pytest

from transmux import rag


def test_model_uses_cached_snapshot_only(monkeypatch, tmp_path):
    calls = []
    rag.local_model.cache_clear()
    monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
    monkeypatch.setitem(sys.modules, 'sentence_transformers', SimpleNamespace(SentenceTransformer=lambda path, **kw: calls.append(('model', path, kw)) or 'model'))
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(snapshot_download=lambda name, **kw: calls.append(('snapshot', name, kw)) or str(tmp_path)))
    try:
        assert rag.local_model('test/model') == 'model'
        assert calls == [('snapshot', 'test/model', {'local_files_only': True}), ('model', str(tmp_path), {'local_files_only': True})]
    finally:
        rag.local_model.cache_clear()


def test_missing_cache_returns_actionable_error(monkeypatch):
    rag.local_model.cache_clear()
    def missing(*args, **kwargs):
        raise OSError('cache missing')
    monkeypatch.setitem(sys.modules, 'sentence_transformers', SimpleNamespace(SentenceTransformer=lambda *a, **kw: None))
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(snapshot_download=missing))
    with pytest.raises(ValueError, match='任务不会在线下载模型'):
        rag.local_model('missing/model')


def test_local_model_lock_has_bounded_wait(monkeypatch):
    class Busy:
        def acquire(self, timeout):
            assert timeout == 120
            return False
        def release(self):
            raise AssertionError('Must not release another inference lock')
    monkeypatch.setenv('TRANSMUX_EMBEDDING_URL', '')
    monkeypatch.setattr(rag, '_local_inference_lock', Busy())
    with pytest.raises(TimeoutError, match='120 秒'):
        rag.Embeddings().encode(['query'])
