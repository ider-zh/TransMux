"""Extract JCST Publish Word formatting; exclude all example content and identities.

The official file uses direct formatting over generic styles. Paragraph geometry
comes from the representative paragraphs; typography follows their explicit
Times New Roman/point-size instructions (not the generic style names).
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

from lxml import etree

from extract_access_template import P_ORDER, R_ORDER, W

# Zero-based paragraph positions in the verified 2022-06-28 Publish template.
# role: (paragraph, expected prefix, point size, bold, italic)
SAMPLES = {
    'title': (0, 'Journal of Computer Science', 16, True, False),
    'authors': (1, 'First Author', 12, False, False),
    'affiliation': (2, '1 Institute', 12, False, True),
    'abstract': (7, 'Abstract', 12, False, False),
    'keywords': (8, 'Keywords', 12, False, False),
    'body': (11, 'Journal of Computer Science', 12, False, False),
    'heading1': (10, 'Introduction', 12, True, False),
    'heading2': (16, 'Text', 12, True, False),
    'heading3': (26, '2.2.1', 12, False, True),
    'reference_heading': (144, 'References', 12, True, False),
    'reference': (145, '[1]', 12, False, False),
    'caption': (81, 'Fig.1.', 10, False, False),
    'table_caption': (44, 'Table 1', 10, False, False),
    'biography': (154, 'Photo of the first author', 12, False, False),
    'frontmatter': (5, 'E-mail:', 10, False, False),
}
P_ALLOWED = {'keepNext', 'keepLines', 'widowControl', 'snapToGrid', 'spacing', 'ind', 'jc'}


def fragment(node):
    node = deepcopy(node)
    etree.cleanup_namespaces(node)
    return etree.tostring(node, encoding='unicode', with_tail=False)


def extract(source, destination):
    with ZipFile(source) as archive:
        doc = etree.fromstring(archive.read('word/document.xml'))
        styles = etree.fromstring(archive.read('word/styles.xml'))
    by_id = {s.get(W+'styleId'): s for s in styles.findall(W+'style')}
    default = next(s for s in by_id.values() if s.get(W+'default') == '1' and s.get(W+'type') == 'paragraph')

    def merge(result, element):
        if element is not None:
            for prop in element:
                key = etree.QName(prop).localname
                if key in P_ALLOWED:
                    if key in result:
                        result[key].attrib.update(prop.attrib)
                    else:
                        result[key] = deepcopy(prop)
        return result

    def paragraph_style(sid):
        style = by_id[sid]
        parent = style.find(W+'basedOn')
        result = paragraph_style(parent.get(W+'val')) if parent is not None else {}
        return merge(result, style.find(W+'pPr'))

    paragraphs = doc.find(W+'body').findall(W+'p')
    rules = {}
    for role, (index, prefix, size, bold, italic) in SAMPLES.items():
        p = paragraphs[index]
        text = ''.join(p.itertext())
        if not text.startswith(prefix):
            raise ValueError(f'Official template changed at paragraph {index}; review role {role}')
        style = p.find(W+'pPr/'+W+'pStyle')
        sid = style.get(W+'val') if style is not None else default.get(W+'styleId')
        props = merge(paragraph_style(sid), p.find(W+'pPr'))
        # A direct first-line reset cancels inherited hanging indentation.
        if 'ind' in props and props['ind'].get(W+'firstLine') is not None:
            props['ind'].attrib.pop(W+'hanging', None)
        # Author paragraphs use heading styles in the sample solely for typography.
        if role not in {'title', 'heading1', 'heading2', 'heading3', 'reference_heading'}:
            props.pop('keepNext', None)
            props.pop('keepLines', None)
        run = {
            'rFonts': {'ascii':'Times New Roman', 'hAnsi':'Times New Roman', 'cs':'Times New Roman', 'eastAsia':'宋体'},
            'b': {'val':'1' if bold else '0'}, 'bCs': {'val':'1' if bold else '0'},
            'i': {'val':'1' if italic else '0'}, 'iCs': {'val':'1' if italic else '0'},
            'sz': {'val':str(size*2)}, 'szCs': {'val':str(size*2)},
        }
        rules[role] = {'source_paragraph':index+1, 'source_style':sid,
                       'p':[fragment(props[k]) for k in P_ORDER if k in props],
                       'r':[fragment(etree.Element(W+k, {W+a:v for a,v in run[k].items()}, nsmap={'w':W[1:-1]}))
                            for k in R_ORDER if k in run]}
    sections = [[fragment(p) for p in section if etree.QName(p).localname in ('pgSz','pgMar','cols','docGrid')]
                for section in doc.iter(W+'sectPr')]
    if len(sections) != 2:
        raise ValueError('Official section structure changed; review template')
    data = {'id':'jcst', 'version':'2022.06-r1', 'name':'JCST · 出版排版草稿',
            'source_url':'https://wqketang.cn-beijing.oss.aliyuncs.com/office/journal_prod/2022-09-27/e722dd5d-62c0-4b75-b701-4b3811edcfad.docx',
            'guidelines_url':'https://www.sciopen.com/journal/join_journal/submission_guidelines?id=1574306362798288898&issn=1000-9000',
            'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'verified_on':'2026-09-16', 'rules':rules, 'sections':sections}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    extract(Path(sys.argv[1]), Path(sys.argv[2]))
