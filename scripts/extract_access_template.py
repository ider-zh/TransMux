"""Extract formatting only from the official Word template; never ship sample prose."""
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

from lxml import etree

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
ROLES = {'title':'PaperTitle', 'authors':'AU', 'affiliation':'PINoSpace', 'abstract':'Abstract',
         'keywords':'IT', 'body':'PARAIndent', 'heading1':'H1', 'heading2':'H2AfterH1',
         'heading3':'H3', 'reference_heading':'H1', 'reference':'References',
         'caption':'FigureCaption', 'table_caption':'TableTitle', 'biography':'AUBios', 'frontmatter':'PI'}
P_ORDER = 'pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr sectPr pPrChange'.split()
R_ORDER = 'rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath rPrChange'.split()


def ordered(properties, order):
    return [properties[key] for key in sorted(properties, key=lambda key: order.index(key))]


def extract(source, destination):
    with ZipFile(source) as z:
        styles = etree.fromstring(z.read('word/styles.xml'))
        document = etree.fromstring(z.read('word/document.xml'))
    by_id = {s.get(W+'styleId'): s for s in styles.findall(W+'style')}

    def properties(sid, tag):
        style = by_id[sid]
        parent = style.find(W+'basedOn')
        result = properties(parent.get(W+'val'), tag) if parent is not None else {}
        element = style.find(W+tag)
        if element is not None:
            for prop in element:
                key = etree.QName(prop).localname
                # Numbering is deliberately retained from the manuscript in this stage.
                if key not in ('numPr', 'rStyle', 'sectPr'):
                    result[key] = etree.tostring(prop, encoding='unicode', with_tail=False)
        return result

    defaults = styles.find(W+'docDefaults').find(W+'rPrDefault').find(W+'rPr')
    default_r = {etree.QName(p).localname: etree.tostring(p, encoding='unicode') for p in defaults}
    rules = {}
    for role, sid in ROLES.items():
        rules[role] = {'source_style': sid, 'p': ordered(properties(sid, 'pPr'), P_ORDER),
                       'r': ordered(default_r | properties(sid, 'rPr'), R_ORDER)}
    sections = document.findall('.//'+W+'sectPr')
    section_rules = [[etree.tostring(p, encoding='unicode') for p in section
                      if etree.QName(p).localname in ('pgSz','pgMar','cols','docGrid')]
                     for section in sections[:2]]
    data = {'id':'ieee-access', 'version':'2024.04-r1', 'name':'IEEE Access · 投稿排版草稿',
            'source_url':'https://ieeeaccess.ieee.org/wp-content/uploads/2025/08/Access-Template-2024.docx',
            'guidelines_url':'https://ieeeaccess.ieee.org/authors/submission-guidelines/',
            'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'verified_on':'2026-09-16', 'rules':rules, 'sections':section_rules}
    # Strip redundant namespace declarations, keeping valid independent XML fragments.
    for rule in [v[k] for v in rules.values() for k in ('p','r')] + section_rules:
        for i, fragment in enumerate(rule):
            node = etree.fromstring(fragment.encode())
            etree.cleanup_namespaces(node)
            rule[i] = etree.tostring(node, encoding='unicode')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    extract(Path(sys.argv[1]), Path(sys.argv[2]))
