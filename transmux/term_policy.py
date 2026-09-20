"""Selection policy shared by extraction, translation and legacy screening."""
from . import terminology
from .languages import guidance_language_error


POLICY = """
Terminology selection policy:
Collect only expressions with a concrete consistency need: competing preferred forms,
domain ambiguity, an abbreviation needing a stable expansion, or an explicitly named
project concept. Ordinary vocabulary and general definitions alone do not qualify.
Do not decide from word length or apparent difficulty. For example, a definition of
Script as Schank's representational concept is NOT enough to collect it. Collect it
only if this project needs a fixed form or disambiguation, with a narrowly stated scope.
Every selected term/pair requires need, reason and scope/context supported by the input.
Avoid turning one paper's definition into a universal rule. Empty arrays are valid.
Keep people out of terms and mappings; report them in people instead.
Person-name policy:
Preserve a known approved target spelling consistently within its stated context.
Full names, surnames and initials can follow the prose, but must denote the same person.
Do not infer a full name from a surname, merge people by surname, invent aliases, or
change bibliography author formatting. When reliable correspondence is unavailable,
retain the source spelling in the draft, leave the proposed translation empty if unknown,
and set identity_confirmed=false. Do not block the translation merely for an unresolved
name. An unsupported guessed name used in the actual draft IS a translation error.
Only active terminology is mandatory; pending and inactive entries are not rules.
The current active snapshot supersedes older terminology instructions in session history.
Do not treat a remembered rule absent from this snapshot as mandatory.
User-approved rules take precedence. Never override them with a newly extracted guess.
"""


def metadata(row):
    return {key: row[key] for key in ('scope', 'reason', 'need', 'evidence') if isinstance(row.get(key), str)}


def quote(text, term):
    index = text.casefold().find(term.casefold())
    return text[max(0, index - 150):max(0, index) + len(term) + 250]


def screen(rows, target):
    kept, report = [], []
    for row in rows:
        error = terminology.admission(row)
        if not error:
            for field in ('scope', 'reason'):
                error = guidance_language_error(row.get(field, ''), target)
                if error:
                    break
        if error:
            report.append({'status': 'skipped', 'candidate': row, 'reason': error})
        else:
            kept.append({**row, 'status': 'active'})
    return kept, report


def names(rows, texts, target, existing, groups=None):
    """Unknown cross-language identities remain proposals; never bless a guess."""
    accepted, report = [], []
    if not isinstance(rows, list):
        return [], [{'status': 'skipped', 'reason': 'Invalid people array'}]
    for row in rows:
        try:
            fields = ('original', 'translation', 'aliases', 'context', 'reason')
            if not isinstance(row, dict) or any(not isinstance(row.get(f), str) or len(row[f]) > 10000 for f in fields):
                raise ValueError('Invalid person fields')
            original = row['original'].strip()
            if not original:
                raise ValueError('Empty person name')
            if groups is None:
                index = row.get('paragraph')
                if type(index) is not int or not 1 <= index <= len(texts):
                    raise ValueError('Invalid name source paragraph')
                source = texts[index - 1]
                cited = str(index)
                group_index = None
            else:
                matches = [(i, g) for i, g in enumerate(groups)
                           if row.get('source_id') in g['source_ids'] and row.get('target_id') in g['target_ids']]
                if len(matches) != 1:
                    raise ValueError('Invalid name source/target IDs')
                group_index, group = matches[0]
                source = group['original_paragraphs'][group['source_ids'].index(row['source_id'])]
                cited = row['source_id']
            if original.casefold() not in source.casefold():
                raise ValueError('Name absent from source evidence')
            if any(guidance_language_error(row[f], target) for f in ('context', 'reason')):
                raise ValueError('Name context/reason must use the target language')
            proposed = row['translation'].strip()
            same = proposed == original
            known = any(terminology.active(p) and p['original'].casefold() == original.casefold()
                        and p['translation'] == proposed and p.get('context', '').casefold() == row['context'].casefold()
                        for p in existing)
            confirmed = row.get('identity_confirmed') is True and bool(proposed) and (same or known)
            aliases = '; '.join(a.strip() for a in row['aliases'].split(';')
                                if a.strip() and a.strip().casefold() in source.casefold()) if confirmed else ''
            result = {f: row[f] for f in fields}
            result.update(aliases=aliases, status='active' if confirmed else 'pending',
                          evidence=quote(source, original), source=f'原文 {cited}', _group=group_index)
            accepted.append(result)
        except ValueError as exc:
            report.append({'status': 'skipped', 'candidate': row, 'reason': str(exc)})
    return accepted, report
