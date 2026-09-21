"""Run synthetic translation and externally sourced fact checking with a real CLI."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from transmux.v2 import WorkspaceStore, WorkspaceWorker


async def main(agent, model):
    root = Path(tempfile.mkdtemp(prefix=f'v2-{agent}-', dir=Path('build').resolve()))
    store = WorkspaceStore(root)
    pid = store.create_project('V2 acceptance', agent, model)['id']
    worker = WorkspaceWorker(store)
    path = store.workspace(pid) / 'sources' / 'fixture.txt'
    path.write_text('巴黎是法国的首都。')
    Path(str(path) + '.json').write_text(json.dumps(['巴黎是法国的首都。']))
    fid = store.add_file(pid, 'fixture.txt', 'source', path)
    for kind in ('translate', 'factcheck'):
        payload = {'file_ids': [fid], 'message': 'Translate faithfully.' if kind == 'translate' else 'Check the factual claim using an authoritative external source.',
                   'use_rag': False, 'max_review_rounds': 3, 'external_research': kind == 'factcheck', 'glossary_ids': []}
        job = store.enqueue(pid, kind, payload)
        print(json.dumps({'agent': agent, 'kind': kind, 'root': str(root), 'job': job['id']}), flush=True)
        await worker.execute(job)
        row = store.rows('SELECT state,result FROM jobs WHERE id=?', (job['id'],))[0]
        print(json.dumps(row, ensure_ascii=False), flush=True)
        assert row['state'] == 'succeeded', row
        if kind == 'translate':
            fid = json.loads(row['result'])['documents'][0]['file_id']
            # Synthetic acceptance fixture: explicitly approve before the downstream test.
            store.approve_review(pid, store.review_for_file(pid, fid)['id'])
    report = {'agent': agent, 'model': model, 'root': str(root), 'status': 'passed', 'rag': worker.rag}
    (root / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    store.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('agent', choices=['codex', 'codebuddy'])
    parser.add_argument('--model')
    args = parser.parse_args()
    asyncio.run(main(args.agent, args.model))
