"""Opt-in real-agent acceptance, using isolated synthetic data only."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

import numpy as np

from transmux import terminology, term_review
from transmux.documents import extract
from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import Store


class LocalTestEmbeddings:
    identity = 'acceptance-only-fixed-vectors'

    def encode(self, texts):
        return np.array([[1., 0.] for _ in texts], dtype=np.float32)


async def main(agent, model):
    root = Path(tempfile.mkdtemp(prefix=f'terminology-{agent}-', dir=Path('build').resolve()))
    store = Store(root)
    pid = store.create_project('Terminology acceptance', agent, model=model)['id']
    work = store.workspace(pid)
    reference = work / 'corpus' / 'reference.txt'
    reference.write_text('Script is a representational concept associated with Schank and described as resembling Minsky’s Frame. '
                         'This passage explains historical concepts without prescribing a preferred term.\n'
                         'For this project, consistently use retrieval-augmented generation (RAG) on first mention and RAG thereafter. '
                         'Do not alternate with retrieval augmented generation or retrieval-enhanced generation.\n'
                         'Marvin Minsky and Roger Schank are separate researchers. Retain these exact name spellings.\n'
                         'The experiment uses data. Results are presented in a table.\n')
    Path(str(reference) + '.json').write_text(json.dumps(extract(reference)))
    store.add_file(pid, reference.name, 'corpus', reference)
    names = store.snapshot_config(pid, 'people')
    document = terminology.decode('people', names['content'])
    document['rows'].append(dict(original='马文·明斯基', translation='Marvin Minsky', aliases='Minsky', context='Artificial intelligence research',
                                 source='User-approved test fixture', origin='manual', status='active'))
    store.write_config(pid, 'people', terminology.encode(document), names['revision'])
    worker = Worker(store, rag=Rag(LocalTestEmbeddings()))

    async def execute(kind, payload):
        task = store.enqueue(pid, kind, {**payload, 'use_rag': False, 'max_review_rounds': 3})
        print(json.dumps(dict(agent=agent, kind=kind, root=str(root), job=task['id'])), flush=True)
        await worker.execute(task)
        finished = store.rows('SELECT state,result FROM jobs WHERE id=?', (task['id'],))[0]
        print(json.dumps(finished, ensure_ascii=False), flush=True)
        assert finished['state'] == 'succeeded', finished
        return task, finished

    await execute('style', {})
    terms = terminology.decode('terms', store.snapshot_config(pid, 'terms')['content'])['rows']
    assert not any(r['term'].casefold() in ('script','data','table','experiment','frame') for r in terms), terms
    assert any('rag' in r['term'].casefold() or 'retrieval' in r['term'].casefold() for r in terms), terms
    assert all(r.get('reason') and r.get('scope') and r.get('evidence') for r in terms)
    source = work / 'sources' / 'source.txt'
    source.write_text('马文·明斯基讨论了知识表示。王海青参与了本次实验。系统使用检索增强生成。')
    Path(str(source) + '.json').write_text(json.dumps(extract(source)))
    fid = store.add_file(pid, source.name, 'source', source)
    _, finished = await execute('translate', {'file_id': fid})
    output_id = json.loads(finished['result'])['file_id']
    output = store.download_path(pid, store.file(pid, output_id))
    text = '\n'.join(extract(output))
    assert 'Marvin Minsky' in text and '王海青' in text, text
    people = terminology.decode('people', store.snapshot_config(pid, 'people')['content'])['rows']
    assert any(r['original'] == '王海青' and r['status'] == 'pending' for r in people), people
    assert any(r['original'] == '马文·明斯基' and r['translation'] == 'Marvin Minsky' and r['origin'] == 'manual' for r in people)
    # Simulate an existing low-value entry, then verify screening is preview-only.
    store.merge_terminology(pid, 'terms', [dict(term='Script', meaning='A representational concept associated with Schank and resembling Minsky’s Frame.',
                                              usage='A general definition only.', source='Historical extraction')], 'extraction', 'legacy-fixture')
    before = {k: store.snapshot_config(pid,k) for k in terminology.KINDS}
    task, _ = await execute('terminology_review', {})
    assert before == {k: store.snapshot_config(pid,k) for k in terminology.KINDS}
    proposal = json.loads((work / 'runs' / task['id'] / 'terminology-proposal.json').read_text())
    script = next(item for item in proposal['items'] if item['kind'] == 'terms' and item['row']['term'] == 'Script')
    assert script['action'] == 'disable', script
    docs = {k: terminology.decode(k, s['content']) for k,s in before.items()}
    updated = term_review.apply_proposal(docs, proposal, [dict(index=script['index'], edits={})])
    store.write_terminology_bundle(pid, updated, proposal['revisions'], task['id'])
    assert not terminology.active(next(r for r in terminology.decode('terms',store.snapshot_config(pid,'terms')['content'])['rows'] if r['term']=='Script'))
    report = dict(state='passed', agent=agent, model=model, root=str(root), output=str(output), terms=[r['term'] for r in terms],
                  unknown_person='王海青: preserved and pending', screening='preview unchanged; applied Script deactivation')
    (root / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    store.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('agent', choices=['codex','codebuddy'])
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    asyncio.run(main(args.agent, args.model))
