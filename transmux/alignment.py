"""Ordered source coverage independent of target paragraph count."""
import json
import re

from docx import Document

from .documents import doc_paragraphs, paragraph_structure
from .sections import ChapterPlanner


def source_blocks(source, texts):
    metadata = paragraph_structure(source, len(texts))
    doc = Document(source) if source.suffix.lower() == '.docx' else None
    paragraphs = ([p for p in doc_paragraphs(doc) if p.text.strip()]
                  if source.suffix.lower() == '.docx' else None)
    if paragraphs is not None and [p.text for p in paragraphs] != texts:
        raise ValueError('原文与上传时的文本快照不一致')
    blocks, segment, previous, fence = [], 0, None, None
    for i, (text, meta) in enumerate(zip(texts, metadata), 1):
        protected = meta['kind'] != 'paragraph'
        protected |= bool(re.match(r'^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|[图表]\s*\d|(?:Figure|Fig\.|Table)\s+\d)', text, re.I))
        if paragraphs is not None:
            p = paragraphs[i - 1]
            styles, style = [], p.style
            while style is not None and style.style_id not in [s.style_id for s in styles]:
                styles.append(style)
                style = style.base_style
            if meta['kind'] != 'table':
                outline = p._p.xpath('./w:pPr/w:outlineLvl/@w:val')
                for inherited in styles:
                    if outline:
                        break
                    outline = inherited.element.xpath('./w:pPr/w:outlineLvl/@w:val')
                if outline and outline[0].isdigit() and 0 <= int(outline[0]) <= 8:
                    meta = {'kind': 'heading', 'level': int(outline[0]) + 1}
                    protected = True
            if any(s.element.xpath('.//w:numPr | .//w:outlineLvl') for s in styles) or p._p.xpath('./w:pPr/w:numPr | ./w:pPr/w:outlineLvl'):
                protected = True
            if any(re.search(r'heading|title|caption|list|标题|题注|列表', s.name or '', re.I) for s in styles):
                protected = True
            # Only ordinary text runs may be removed or duplicated. Fields, links,
            # drawings, bookmarks, section/page breaks and wrappers are protected.
            safe_tags = {'pPr', 'r', 'rPr', 't', 'tab', 'cr', 'br'}
            unsafe = any(el.tag.split('}')[-1] not in safe_tags
                         for el in p._p.iterdescendants()
                         if not any(a.tag.endswith(('}pPr', '}rPr')) for a in el.iterancestors()))
            protected |= unsafe or bool(p._p.xpath('.//w:sectPr | .//w:br[@w:type="page" or @w:type="column"] | .//w:pageBreakBefore'))
            protected |= any(s.paragraph_format.page_break_before for s in styles)
            protected |= p._p.getparent() is not doc.element.body
            adjacent = previous is not None and previous.getnext() is p._p
            previous = p._p
        else:
            adjacent = True
            marker = re.match(r'^\s*(`{3,}|~{3,})', text)
            in_code = fence is not None or marker is not None
            if marker:
                if fence is None:
                    fence = marker[1][0]
                elif marker[1][0] == fence:
                    fence = None
            protected |= in_code
            # Only explicit text heading markers. Plain numbered lines may be lists.
            if not in_code and len(text) <= 160 and '\n' not in text:
                markdown = re.fullmatch(r'\s*(#{1,6})\s+\S.*', text)
                chapter = re.fullmatch(r'\s*(?:第[零〇一二三四五六七八九十百千\d]+章(?:(?:\s+|[:：、\-])[^。！？.!?]+)?|Chapter\s+\d+(?:\s*[:：\-]\s*[^。！？.!?]+)?)\s*', text, re.I)
                section = re.fullmatch(r'\s*(?:第[零〇一二三四五六七八九十百千\d]+节(?:(?:\s+|[:：、\-])[^。！？.!?]+)?|Section\s+\d+(?:\.\d+)*(?:\s*[:：\-]\s*[^。！？.!?]+)?)\s*', text, re.I)
                if markdown or chapter or section:
                    meta = {'kind': 'heading', 'level': len(markdown[1]) if markdown else 1 if chapter else 2}
                    protected = True
        if not adjacent or protected or (blocks and blocks[-1]['protected']):
            segment += 1
        blocks.append({**meta, 'id': f'p{i:06d}', 'position': i, 'text': text,
                       'protected': bool(protected), 'segment': segment})
    return blocks


def window(blocks, offset, max_chars=10000):
    return ChapterPlanner(blocks, max_chars=max_chars).window(offset)


def normalize_groups(value, blocks, continuation=False):
    if isinstance(value, str):
        value = json.loads(value)
    # Compatibility for older CLI responses; still requires complete 1:1 coverage.
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value) and len(value) == len(blocks):
        value = [dict(source_ids=[b['id']], paragraphs=[v], reason='') for b, v in zip(blocks, value)]
    if not isinstance(value, list) or not value:
        raise ValueError('translations must be a nonempty array of paragraph groups')
    cursor, rows = 0, []
    for group in value:
        if not isinstance(group, dict):
            raise ValueError('Each translation group must be an object')
        ids, texts, reason = group.get('source_ids'), group.get('paragraphs'), group.get('reason')
        if not isinstance(ids, list) or not ids or ids != [b['id'] for b in blocks[cursor:cursor + len(ids)]]:
            raise ValueError('Source IDs must cover the batch exactly once, in order, without gaps or duplicates')
        if not isinstance(texts, list) or not texts or any(not isinstance(t, str) or not t.strip() or '\n' in t or '\r' in t for t in texts):
            raise ValueError('Use separate nonempty strings for target paragraphs; do not embed newlines')
        selected = blocks[cursor:cursor + len(ids)]
        changed = len(ids) != 1 or len(texts) != 1
        if changed and (any(b['protected'] for b in selected) or len({b['segment'] for b in selected}) != 1):
            raise ValueError('Cannot restructure protected blocks or cross structural boundaries')
        if not isinstance(reason, str) or (changed and not reason.strip()):
            raise ValueError('Explain every merge or split in reason')
        first = selected[0]
        meta = {k: v for k, v in first.items() if k not in ('text', 'position', 'id')}
        rows.append({**meta, 'id': first['id'], 'source_ids': ids,
                     'source_positions': [b['position'] for b in selected],
                     'original_paragraphs': [b['text'] for b in selected],
                     'target_ids': [f"{first['id']}:t{i}" for i in range(1, len(texts) + 1)],
                     'translations': texts, 'reason': reason,
                     'original': '\n\n'.join(b['text'] for b in selected), 'translation': '\n\n'.join(texts)})
        cursor += len(ids)
    if cursor != len(blocks):
        raise ValueError('Missing source paragraphs')
    if continuation and len(rows) < 2:
        raise ValueError('This window continues: return at least two groups so its trailing group can be reconsidered with the next window')
    return rows


def target_texts(rows):
    return [text for row in rows for text in row.get('translations', [row['translation']])]


def group_mappings(worker, candidates, groups, target):
    """Evidence may span source/target paragraphs, but only within one group."""
    if not isinstance(candidates, list):
        return [], [{'status': 'skipped', 'reason': 'Invalid mappings array'}]
    accepted, report = [], []
    source = [row['original'] for row in groups]
    translated = [row['translation'] for row in groups]
    for candidate in candidates:
        if isinstance(candidate, dict) and ('source_id' in candidate or 'target_id' in candidate):
            matches = [i for i, g in enumerate(groups, 1)
                       if candidate.get('source_id') in g['source_ids'] and candidate.get('target_id') in g['target_ids']]
            if len(matches) != 1:
                report.append({'status': 'skipped', 'candidate': candidate, 'reason': 'Invalid source/target evidence IDs'})
                continue
            index = matches[0]
            g = groups[index - 1]
            s = g['original_paragraphs'][g['source_ids'].index(candidate['source_id'])]
            t = g['translations'][g['target_ids'].index(candidate['target_id'])]
            checked, diagnostics = worker.filter_mappings([{**candidate, 'paragraph': 1}], [s], [t], target)
            if checked:
                accepted.append({**checked[0], 'paragraph': index, 'source_id': candidate['source_id'], 'target_id': candidate['target_id']})
        else:
            checked, diagnostics = worker.filter_mappings([candidate], source, translated, target)
            accepted.extend(checked)
        report.extend(diagnostics)
    return accepted, report
