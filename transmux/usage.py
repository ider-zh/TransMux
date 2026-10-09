"""Durable CLI usage accounting and a shared Kimi K3 price benchmark."""
import json
import time
import uuid


PRICING = {
    'model': 'kimi-k3', 'currency': 'CNY', 'per_tokens': 1_000_000,
    'input': 20, 'cached_input': 2, 'output': 100,
    'checked_on': '2026-10-08', 'source': 'https://platform.kimi.com/docs/pricing/chat',
    'note': '统一按 Kimi K3 估算，非实际账单；缓存写入按默认 5min 档归入普通输入，不计工具费用。',
}
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens')


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def terminal_usage(event, engine):
    if event.get('type') not in ('turn.completed', 'result'):
        return None
    usage = event.get('usage')
    if not isinstance(usage, dict) or not any(key in usage for key in ('input_tokens', 'output_tokens')):
        return None
    cached = count(usage.get('cached_input_tokens', usage.get('cache_read_input_tokens')))
    total_input = count(usage.get('input_tokens'))
    output = count(usage.get('output_tokens'))
    # CodeBuddy's modelUsage separates normal input, cache reads and cache writes.
    # Its top-level input_tokens already includes these categories.
    models = event.get('modelUsage')
    if engine == 'codebuddy' and isinstance(models, dict) and models and all(
            isinstance(m, dict) and 'inputTokens' in m and 'outputTokens' in m for m in models.values()):
        cached = sum(count(m.get('cacheReadInputTokens')) for m in models.values())
        total_input = sum(count(m.get('inputTokens')) + count(m.get('cacheReadInputTokens'))
                          + count(m.get('cacheCreationInputTokens')) for m in models.values())
        output = sum(count(m.get('outputTokens')) for m in models.values())
    return dict(input_tokens=max(total_input, cached), cached_input_tokens=cached, output_tokens=output)


def cost_micro_cny(values, pricing=PRICING):
    return ((values['input_tokens'] - values['cached_input_tokens']) * pricing['input']
            + values['cached_input_tokens'] * pricing['cached_input'] + values['output_tokens'] * pricing['output'])


def initialize(store):
    store.db.executescript('''
        CREATE TABLE IF NOT EXISTS agent_usage (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, job TEXT NOT NULL, engine TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0, cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, reported INTEGER NOT NULL DEFAULT 0,
            complete INTEGER NOT NULL DEFAULT 0, cost_micro_cny INTEGER NOT NULL DEFAULT 0,
            pricing TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS usage_job ON agent_usage(project, job);
    ''')
    if 'usage_tracking' not in {r[1] for r in store.db.execute('PRAGMA table_info(jobs)')}:
        store.execute('ALTER TABLE jobs ADD COLUMN usage_tracking INTEGER NOT NULL DEFAULT 0')
    # Only reconstruct terminal usage, never add streamed message usage to it.
    for job in store.rows('''SELECT j.id,j.project,p.agent FROM jobs j JOIN projects p ON p.id=j.project
                            WHERE j.usage_tracking=0 AND j.state NOT IN ('running','queued')'''):
        for event in store.rows("SELECT id,text FROM events WHERE job=? AND kind='agent' ORDER BY id", (job['id'],)):
            try:
                raw = json.loads(event['text'])
                values = terminal_usage(raw, job['agent']) if isinstance(raw, dict) else None
            except (ValueError, TypeError):
                continue
            if values:
                store.execute('''INSERT OR IGNORE INTO agent_usage
                    (id,project,job,engine,input_tokens,cached_input_tokens,output_tokens,reported,complete,cost_micro_cny,pricing,created)
                    VALUES (?,?,?,?,?,?,?,1,0,?,?,?)''',
                    (f"legacy-{event['id']}",job['project'],job['id'],job['agent'],*(values[k] for k in FIELDS),
                     cost_micro_cny(values),json.dumps(PRICING),time.time()))
        store.execute('UPDATE jobs SET usage_tracking=-1 WHERE id=?', (job['id'],))


class UsageCall:
    def __init__(self, store, pid, jid, engine):
        self.store, self.pid, self.jid, self.engine = store, pid, jid, engine
        self.id = uuid.uuid4().hex
        self.values = dict.fromkeys(FIELDS, 0)
        self.messages, self.seen = {}, set()
        self.reported = self.complete = False
        store.execute('''INSERT INTO agent_usage (id,project,job,engine,pricing,created) VALUES (?,?,?,?,?,?)''',
                      (self.id,pid,jid,engine,json.dumps(PRICING),time.time()))

    def consume(self, event):
        values = terminal_usage(event, self.engine)
        if values is not None:
            signature = json.dumps(event, sort_keys=True)
            if signature in self.seen:
                return
            self.seen.add(signature)
            if self.engine == 'codex' and self.complete:
                self.values = {key: self.values[key] + values[key] for key in FIELDS}
            else:
                self.values = values
            self.reported = self.complete = True
        elif self.engine == 'codebuddy' and event.get('type') == 'assistant' and not self.complete:
            message = event.get('message', {})
            usage = message.get('usage')
            identifier = message.get('id') or event.get('_messageId')
            if not identifier or not isinstance(usage, dict):
                return
            cached = count(usage.get('cache_read_input_tokens'))
            values = dict(input_tokens=max(count(usage.get('input_tokens')), cached + count(usage.get('cache_creation_input_tokens'))),
                          cached_input_tokens=cached, output_tokens=count(usage.get('output_tokens')))
            previous = self.messages.get(identifier, dict.fromkeys(FIELDS, 0))
            self.messages[identifier] = {key: max(previous[key], values[key]) for key in FIELDS}
            self.values = {key: sum(v[key] for v in self.messages.values()) for key in FIELDS}
            self.reported = True
        else:
            return
        self.store.execute('''UPDATE agent_usage SET input_tokens=?,cached_input_tokens=?,output_tokens=?,
            reported=?,complete=?,cost_micro_cny=? WHERE id=?''',
            (*(self.values[k] for k in FIELDS),int(self.reported),int(self.complete),cost_micro_cny(self.values),self.id))


def summary(store, pid):
    store.project(pid)
    jobs = {j['id']: dict.fromkeys(FIELDS, 0) | {
        'calls': 0, 'reported_calls': 0, 'incomplete_calls': 0, 'cost_micro_cny': 0,
        'tracking': j['usage_tracking'] == 1,
    } for j in store.rows('SELECT id,usage_tracking FROM jobs WHERE project=?', (pid,))}
    for row in store.rows('SELECT * FROM agent_usage WHERE project=?', (pid,)):
        if row['job'] not in jobs:
            continue
        target = jobs[row['job']]
        for key in (*FIELDS, 'cost_micro_cny'):
            target[key] += row[key]
        target['calls'] += 1
        target['reported_calls'] += row['reported']
        target['incomplete_calls'] += not row['complete']
    total = dict.fromkeys((*FIELDS, 'calls', 'reported_calls', 'incomplete_calls', 'cost_micro_cny', 'untracked_jobs'), 0)
    for job in jobs.values():
        for key in (*FIELDS, 'calls', 'reported_calls', 'incomplete_calls', 'cost_micro_cny'):
            total[key] += job[key]
        total['untracked_jobs'] += not job['tracking']
    for row in [*jobs.values(), total]:
        row['total_tokens'] = row['input_tokens'] + row['output_tokens']
        row['estimated_cny'] = row['cost_micro_cny'] / 1_000_000
    return {'pricing': PRICING, 'jobs': jobs, 'total': total}
