import asyncio

import pytest

from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import Store
from test_workflows import TinyEmbeddings


async def wait_until(check):
    async def wait():
        while not check():
            await asyncio.sleep(.005)
    await asyncio.wait_for(wait(), 3)


async def test_cancel_waits_for_project_cleanup_without_blocking_other_projects(tmp_path):
    store = Store(tmp_path)
    a = store.create_project('A', 'codex')['id']
    b = store.create_project('B', 'codebuddy')['id']
    first = store.enqueue(a, 'chat', {})
    following = store.enqueue(a, 'chat', {})
    other = store.enqueue(b, 'chat', {})
    entered, cleaning = set(), asyncio.Event()
    release, cleaned = asyncio.Event(), asyncio.Event()

    class ControlledWorker(Worker):
        async def perform(self, job):
            entered.add(job['id'])
            if job['id'] == first['id']:
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await cleaned.wait()
            elif job['id'] == other['id']:
                await release.wait()
            return 'done'

    worker = ControlledWorker(store, rag=Rag(TinyEmbeddings()))
    loop = asyncio.create_task(worker.loop())
    try:
        await wait_until(lambda: first['id'] in entered and other['id'] in entered)
        assert worker.cancel(a, first['id'])
        await asyncio.wait_for(cleaning.wait(), 2)
        assert worker.cancel(a, first['id'])  # repeated cancellation must not interrupt cleanup
        release.set()
        await wait_until(lambda: other['id'] not in worker.running)
        assert following['id'] not in entered
        cleaned.set()
        await wait_until(lambda: following['id'] in entered and not worker.running)
        states = {j['id']:j['state'] for j in store.rows('SELECT id,state FROM jobs')}
        assert states == {first['id']:'cancelled', following['id']:'succeeded', other['id']:'succeeded'}
    finally:
        release.set()
        cleaned.set()
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop
        store.db.close()


async def test_shutdown_drains_all_projects_and_keeps_queued_jobs(tmp_path):
    store = Store(tmp_path)
    entered, cleaned = set(), set()
    cleanup = asyncio.Event()

    class ControlledWorker(Worker):
        async def perform(self, job):
            entered.add(job['id'])
            try:
                await asyncio.Event().wait()
            finally:
                await cleanup.wait()
                cleaned.add(job['id'])

    first, queued = [], []
    for agent in ('codex', 'codebuddy'):
        pid = store.create_project(agent, agent)['id']
        first.append(store.enqueue(pid, 'chat', {})['id'])
        queued.append(store.enqueue(pid, 'chat', {})['id'])
    worker = ControlledWorker(store, rag=Rag(TinyEmbeddings()))
    loop = asyncio.create_task(worker.loop())
    try:
        await wait_until(lambda: entered == set(first))
        loop.cancel()
        await asyncio.sleep(.02)
        assert not loop.done()
        cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await loop
        assert cleaned == set(first) and not worker.running
        states = {j['id']:j['state'] for j in store.rows('SELECT id,state FROM jobs')}
        assert all(states[jid] == 'cancelled' for jid in first)
        assert all(states[jid] == 'queued' for jid in queued)
    finally:
        cleanup.set()
        if not loop.done():
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
        store.db.close()


async def test_project_failure_releases_its_queue_while_other_project_remains_running(tmp_path):
    store = Store(tmp_path)
    a = store.create_project('A', 'codex')['id']
    b = store.create_project('B', 'codebuddy')['id']
    failed = store.enqueue(a, 'chat', {})['id']
    next_job = store.enqueue(a, 'chat', {})['id']
    other = store.enqueue(b, 'chat', {})['id']
    entered, release = set(), asyncio.Event()

    class ControlledWorker(Worker):
        async def perform(self, job):
            entered.add(job['id'])
            if job['id'] == failed:
                raise RuntimeError('Controlled provider failure')
            if job['id'] == other:
                await release.wait()
            return 'done'

    worker = ControlledWorker(store, rag=Rag(TinyEmbeddings()))
    loop = asyncio.create_task(worker.loop())
    try:
        await wait_until(lambda: next_job in entered and other in entered)
        states = {j['id']:j['state'] for j in store.rows('SELECT id,state FROM jobs')}
        assert states[failed] == 'failed' and states[next_job] == 'succeeded'
        assert states[other] == 'running'
    finally:
        release.set()
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        store.db.close()
