"""One-off legacy screening produces a reviewable proposal, never silent changes."""
import json

from . import terminology as terms
from .term_policy import POLICY
from .store import atomic_write


def object_schema(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


ITEM = object_schema({
    'index': {'type': 'integer'},
    'action': {'type': 'string', 'enum': ['retain', 'update', 'person', 'disable']},
    **{field: {'type': 'string'} for field in ('reason', 'scope', 'meaning', 'usage', 'translation', 'aliases', 'context')},
})


async def generate(worker, job, base, run):
    pid, jid = job['project'], job['id']
    snapshots = {kind: worker.store.snapshot_config(pid, kind) for kind in terms.KINDS}
    records = []
    for kind, snapshot in snapshots.items():
        for index, row in enumerate(terms.decode(kind, snapshot['content'])['rows']):
            if row['origin'] != 'manual' and row.get('status') != 'inactive' and not row.get('variant'):
                records.append(dict(index=len(records), kind=kind, row_index=index, row=row))
    proposals = []
    for batch in worker.batches([json.dumps(r, ensure_ascii=False) for r in records], max_chars=12000):
        sample = [json.loads(s) for s in batch]
        worker.phase(job, 'screening', '重新筛选术语', f'已检查 {len(proposals)} / {len(records)} 条，仅生成预览',
                     total=len(records), completed=len(proposals))
        response = await worker.runner.run(pid, jid, base + POLICY +
            '\nScreen these existing entries. Return exactly one decision for each input index. '
            'Do not invent source evidence or names. Definitions are not evidence of a consistency need. '
            'Use disable for ordinary vocabulary/general definitions with no documented consistency need; '
            'update for a useful term requiring narrower scope; person for personal names; retain when already suitable. '
            'No changes are applied now. Empty unused fields are valid. Write reasons and scope in the target language.\n' +
            json.dumps(sample, ensure_ascii=False), object_schema({'decisions': {'type': 'array', 'items': ITEM}}))
        decisions = response.get('decisions')
        if not isinstance(decisions, list) or sorted(r.get('index', -1) for r in decisions if isinstance(r, dict)) != sorted(r['index'] for r in sample):
            raise ValueError('筛选结果未完整覆盖条目，未修改任何术语')
        for decision in decisions:
            if any(not isinstance(decision.get(f), str) or len(decision[f]) > 10000 for f in ITEM['properties'] if f != 'index'):
                raise ValueError('筛选结果字段无效')
            if decision['action'] not in ('retain', 'update', 'person', 'disable') or not decision['reason'].strip():
                raise ValueError('筛选结果缺少有效操作或理由')
            record = records[decision['index']]
            old, kind = record['row'], record['kind']
            row = dict(old)
            if decision['action'] == 'update':
                for field in (*terms.FIELDS[kind], 'scope'):
                    if field in decision and decision[field].strip():
                        row[field] = decision[field]
                row['reason'] = decision['reason']
            elif decision['action'] == 'disable':
                row.update(status='inactive', reason=decision['reason'])
            elif decision['action'] == 'person':
                row = dict(original=old.get('term', old.get('original', '')),
                           translation=decision['translation'], aliases=decision['aliases'], context=decision['context'],
                           source=old['source'], evidence=old.get('evidence', ''), reason=decision['reason'],
                           origin='manual', status='pending')
            proposals.append({**record, 'action': decision['action'], 'reason': decision['reason'], 'proposed': row})
    proposal = {'revisions': {k: s['revision'] for k, s in snapshots.items()}, 'items': proposals}
    atomic_write(run / 'terminology-proposal.json', json.dumps(proposal, ensure_ascii=False, indent=2))
    return {'proposal_job': jid, 'count': len(proposals), 'message': '筛选建议已生成，请在术语管理中预览后应用；当前术语未更改。'}


def apply_proposal(documents, proposal, decisions):
    if len(decisions) != len({d['index'] for d in decisions}):
        raise ValueError('重复的筛选操作')
    updates = {kind: json.loads(terms.encode(doc)) for kind, doc in documents.items()}
    items = {item['index']: item for item in proposal['items']}
    for decision in decisions:
        item = items.get(decision['index'])
        if item is None:
            raise ValueError('无效筛选条目')
        kind = item['kind']
        old = updates[kind]['rows'][item['row_index']]
        if old['origin'] == 'manual':
            raise ValueError('用户指定条目不能自动筛选替换')
        if item['action'] == 'retain' and not decision.get('edits'):
            continue
        row = {**item['proposed'], **decision.get('edits', {})}
        allowed = {*terms.FIELDS['people' if item['action'] == 'person' else kind], 'scope', 'reason', 'evidence'} - {'source'}
        if set(decision.get('edits', {})) - allowed:
            raise ValueError('无效筛选编辑字段')
        row['origin'] = 'manual'
        if item['action'] == 'person':
            updates[kind]['rows'][item['row_index']] = {**old, 'status': 'inactive', 'origin': 'manual'}
            if any(terms.key('people', r) == terms.key('people', row) for r in updates['people']['rows']):
                raise ValueError('人名已存在，请先处理冲突或跳过此项')
            updates['people']['rows'].append(row)
        else:
            updates[kind]['rows'][item['row_index']] = row
    for kind, doc in updates.items():
        terms.decode(kind, terms.encode(doc))
    return updates
