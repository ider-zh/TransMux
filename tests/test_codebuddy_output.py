import json

import pytest

from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import Store
from test_workflows import FakeRunner, TinyEmbeddings, add_source, job


@pytest.mark.parametrize('encoded,expected_state', [
    (json.dumps(['医生在医院工作。', '保留第二段。']), 'succeeded'),
    (json.dumps(['缺少一段']), 'needs_attention'),
    ('not JSON', 'needs_attention'),
    (json.dumps({'paragraph': 'wrong structure'}), 'needs_attention'),
])
async def test_codebuddy_encoded_arrays_still_require_complete_translation(tmp_path, encoded, expected_state):
    store = Store(tmp_path)
    pid = store.create_project('CodeBuddy test', 'codebuddy')['id']

    class StringRunner(FakeRunner):
        async def run(self, pid, jid, prompt, schema=None):
            if schema and 'translations' in schema['properties']:
                assert schema['properties']['translations']['items']['type'] == 'object'
                assert 'maxItems' not in schema['properties']['translations']
                return {'translations': encoded}
            return await super().run(pid, jid, prompt, schema)

    runner = StringRunner()
    worker = Worker(store, runner, Rag(TinyEmbeddings()))
    task = job(store, pid, add_source(store, pid))
    try:
        await worker.execute(task)
        assert store.rows('SELECT state FROM jobs')[0]['state'] == expected_state
        outputs = store.rows("SELECT * FROM files WHERE kind='output'")
        assert bool(outputs) == (expected_state == 'succeeded')
    finally:
        store.db.close()
