"""Opt-in CLI acceptance for cached, stateless style extraction on synthetic text."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from transmux.agents import AgentRunner
from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import Store
from test_live_terminology import LocalTestEmbeddings


class CountingRunner(AgentRunner):
    calls = 0

    async def run_isolated(self, *args):
        self.calls += 1
        return await super().run_isolated(*args)


async def main(agent, model):
    root = Path(tempfile.mkdtemp(prefix=f'style-{agent}-', dir=Path('build').resolve()))
    store = Store(root)
    pid = store.create_project('Style pipeline acceptance', agent, model=model)['id']
    work = store.workspace(pid)
    reference = work / 'corpus' / 'reference.txt'
    paragraphs = [
        'We evaluate the proposed method on three datasets. We report the mean and standard deviation for each experiment.',
        'The results suggest that the method improves retrieval accuracy. However, the evidence does not establish a causal relationship.',
        'We describe the evaluation procedure before discussing the results. Each section begins with a concise statement of its purpose.',
        'The system uses retrieval-augmented generation (RAG). We use RAG consistently after introducing the full term.',
    ]
    reference.write_text('\n'.join(paragraphs))
    Path(str(reference) + '.json').write_text(json.dumps(paragraphs))
    store.add_file(pid, reference.name, 'corpus', reference)
    store.execute('UPDATE projects SET session=? WHERE id=?', ('preserved-chat-session', pid))
    requirements = store.snapshot_config(pid, 'requirements')
    store.write_config(pid, 'requirements', 'Use British English spelling.', requirements['revision'])
    runner = CountingRunner(store)
    worker = Worker(store, runner, Rag(LocalTestEmbeddings()))
    jobs = []
    for attempt in range(2):
        task = store.enqueue(pid, 'style', {})
        jobs.append(task['id'])
        print(json.dumps({'agent': agent, 'attempt': attempt + 1, 'root': str(root), 'job': task['id']}), flush=True)
        await worker.execute(task)
        finished = store.rows('SELECT state,result FROM jobs WHERE id=?', (task['id'],))[0]
        print(json.dumps(finished, ensure_ascii=False), flush=True)
        assert finished['state'] == 'succeeded', finished
        assert store.project(pid)['session'] == 'preserved-chat-session'
        assert not store.corpus_style_pending(pid)
        if not attempt:
            calls = runner.calls
            assert calls >= 2
        else:
            assert runner.calls == calls, 'Unchanged corpus should reuse extraction and synthesis caches'
    assert store.snapshot_config(pid, 'requirements')['content'] == 'Use British English spelling.'
    report = {'state': 'passed', 'agent': agent, 'model': model, 'root': str(root), 'jobs': jobs,
              'first_run_calls': calls, 'repeat_run_calls': 0, 'chat_session': 'preserved',
              'style': store.snapshot_config(pid, 'style')['content']}
    (root / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    store.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('agent', choices=['codex', 'codebuddy'])
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    asyncio.run(main(args.agent, args.model))
