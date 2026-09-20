"""Workspace terminology documents; provenance and manual decisions survive merges."""
import json
import re
import hashlib


KINDS = ("terms", "mappings", "people")
FIELDS = {
    "terms": ("term", "meaning", "usage", "source"),
    "mappings": ("original", "translation", "context", "source"),
    "people": ("original", "translation", "aliases", "context", "source"),
}
EXTRA = {'scope', 'reason', 'evidence', 'status', 'variant', 'conflict', 'need'}
NEEDS = ('ambiguous', 'preferred_variant', 'abbreviation', 'project_concept')


def active(row):
    return row.get('status', 'active') == 'active'


def base_key(kind, row):
    fields = ('term', 'scope') if kind == 'terms' else ('original', 'context')
    values = [" ".join(row.get(field, '').casefold().split()) for field in fields]
    # Preserve old deletion keys for unscoped terms.
    return values[0] if kind == 'terms' and not values[1] else '\u241f'.join(values)


def encode(document):
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def key(kind, row):
    return base_key(kind, row) + (('\u241e' + row['variant']) if row.get('variant') else '')


def decode(kind, content):
    try:
        data = json.loads(content)
    except (ValueError, TypeError) as exc:
        raise ValueError("术语原文必须是有效 JSON") from exc
    if not isinstance(data, dict) or set(data) != {"rows", "deleted", "legacy"}:
        raise ValueError("术语文档必须包含 rows、deleted、legacy")
    if not isinstance(data["rows"], list) or len(data["rows"]) > 5000:
        raise ValueError("术语 rows 必须是数组，最多 5000 条")
    if not isinstance(data["deleted"], list) or any(not isinstance(x, str) for x in data["deleted"]):
        raise ValueError("deleted 必须是字符串数组")
    if not isinstance(data["legacy"], str):
        raise ValueError("legacy 必须是字符串")
    seen = set()
    for row in data["rows"]:
        if not isinstance(row, dict) or not {*FIELDS[kind], 'origin'} <= set(row) or set(row) - {*FIELDS[kind], 'origin', *EXTRA}:
            raise ValueError("术语行字段不完整")
        if any(not isinstance(value, str) or len(value) > 10000 for value in row.values()):
            raise ValueError("术语字段必须是字符串且不超过 10000 字符")
        required = ("term",) if kind == "terms" else ("original",) if kind == 'people' and not active(row) else ("original", "translation")
        if any(not row[field].strip() for field in required):
            raise ValueError("术语 / 原文 / 译文不能为空")
        if row["origin"] not in ("manual", "extraction", "translation"):
            raise ValueError("无效术语来源")
        if row.get('status', 'active') not in ('active', 'pending', 'inactive'):
            raise ValueError('无效术语状态')
        if active(row) and row.get('variant'):
            raise ValueError('冲突候选需通过确认操作生效')
        identity = key(kind, row)
        if identity in seen:
            raise ValueError("存在重复术语，或相同原文与适用语境的翻译对照")
        seen.add(identity)
    return data


def manual_update(kind, current, proposed, restoring=False):
    old = {key(kind, row): row for row in current["rows"]}
    new = {key(kind, row): row for row in proposed["rows"]}
    for identity, row in new.items():
        if restoring or row != old.get(identity):
            row["origin"] = "manual"
    # Deletions are user decisions too; extraction must not silently undo them.
    proposed["deleted"] = sorted((set(current["deleted"]) | (old.keys() - new.keys())) - new.keys())
    return proposed


def merge(kind, current, candidates, origin):
    rows = {key(kind, row): row for row in current["rows"]}
    added = 0
    for candidate in candidates:
        row = {field: candidate.get(field, "") for field in FIELDS[kind]}
        row.update({field: candidate[field] for field in EXTRA if field in candidate})
        row["origin"] = origin
        identity = key(kind, row)
        deleted = set(current['deleted'])
        if identity in deleted or base_key(kind, row) in deleted or (kind == 'terms' and ' '.join(row['term'].casefold().split()) in deleted):
            continue
        existing = rows.get(identity)
        if existing:
            if existing.get('status') == 'inactive':
                continue
            value_fields = ('meaning', 'usage') if kind == 'terms' else ('translation',)
            differs = any(row.get(f, '').strip().casefold() != existing.get(f, '').strip().casefold() for f in value_fields)
            if not differs:
                continue
            # Preserve the current rule and show the conflicting proposal separately.
            row['status'] = 'pending'
            row['conflict'] = base_key(kind, row)
            row['variant'] = hashlib.sha256(json.dumps([row.get(f, '') for f in value_fields], ensure_ascii=False).encode()).hexdigest()[:16]
            identity = key(kind, row)
            if identity in deleted or rows.get(identity, {}).get('origin') == 'manual' or rows.get(identity, {}).get('status') == 'inactive':
                continue
        added += identity not in rows
        rows[identity] = row
    result = {**current, "rows": list(rows.values())}
    decode(kind, encode(result))
    return result, added


def resolve(kind, document, identity, action):
    selected = next((row for row in document['rows'] if key(kind, row) == identity), None)
    if selected is None:
        raise ValueError('条目不存在，请重新载入')
    rows = [dict(row) for row in document['rows']]
    index = next(i for i, row in enumerate(rows) if key(kind, row) == identity)
    if action == 'activate':
        row = {k: v for k, v in selected.items() if k not in ('variant', 'conflict')}
        row.update(status='active', origin='manual')
        rows = [r for r in rows if key(kind, r) != identity and key(kind, r) != base_key(kind, selected)] + [row]
    elif action == 'deactivate':
        rows[index].update(status='inactive', origin='manual')
    else:
        raise ValueError('无效条目操作')
    result = {**document, 'rows': rows}
    decode(kind, encode(result))
    return result


def admission(candidate):
    """Programmatic minimum evidence; semantic selection remains the Agent's job."""
    if candidate.get('need') not in NEEDS:
        return '没有明确的一致性需求'
    if not isinstance(candidate.get('reason'), str) or not candidate['reason'].strip():
        return '缺少收录理由'
    if not (candidate.get('scope') or candidate.get('context') or '').strip():
        return '缺少适用语境'
    return None


def migrate_legacy(content):
    """Only import explicit old bilingual table rows. Preserve the entire original."""
    rows, seen = [], set()
    active = False
    for line in content.splitlines():
        if not line.strip().startswith("|"):
            active = False
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] in ("原文", "源词", "源术语") and cells[1] in ("译文", "译词", "目标术语"):
            active = True
            continue
        if not active or len(cells) < 2 or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if not cells[0] or not cells[1]:
            continue
        row = dict(original=cells[0], translation=cells[1], context=cells[2] if len(cells) > 2 else "",
                   source="旧关键词对照（保留原值，未重新验证）", origin="manual")
        identity = key("mappings", row)
        if identity not in seen:
            rows.append(row)
            seen.add(identity)
    return dict(rows=rows, deleted=[], legacy=content)
