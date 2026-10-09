"""Official-template formatting rules with conservative structure recognition.

Only document.xml and styles.xml are rewritten; relationships and objects stay intact.
"""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
from zipfile import ZipFile

from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from lxml import etree


TEMPLATES = {key: json.loads((Path(__file__).parent / 'templates' / (key + '.json')).read_text())
             for key in ('ieee-access', 'jcst', 'jcst-submit')}
PRESETS = {key: {name: value for name, value in data.items() if name not in ('rules', 'sections')}
           for key, data in TEMPLATES.items()}
ACCESS, JCST = PRESETS['ieee-access'], PRESETS['jcst']
ROLES = {'title':'标题', 'authors':'作者', 'affiliation':'机构', 'abstract':'摘要', 'keywords':'关键词',
         'body':'正文', 'heading1':'一级标题', 'heading2':'二级标题', 'heading3':'三级标题',
         'reference_heading':'参考文献标题', 'reference':'参考文献条目', 'caption':'图题',
         'table_caption':'表题', 'biography':'作者简介', 'frontmatter':'前置信息', 'keep':'保留原样'}
SECTION_ORDER = ('headerReference','footerReference','footnotePr','endnotePr','type','pgSz','pgMar','paperSrc',
                 'pgBorders','lnNumType','pgNumType','cols','formProt','vAlign','noEndnote','titlePg',
                 'textDirection','bidi','rtlGutter','docGrid','printerSettings','sectPrChange')
RUN_ORDER = 'rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath rPrChange'.split()


def insert_section_property(section, prop):
    tag = etree.QName(prop).localname
    section.insert_element_before(prop, *('w:'+name for name in SECTION_ORDER[SECTION_ORDER.index(tag)+1:]))


def text_of(node):
    return ''.join(node.itertext()) if node.tag == qn('w:t') else ''.join(n.text or '' for n in node.iter(qn('w:t')))


def read_package(source):
    with ZipFile(source) as z:
        if 'word/styles.xml' not in z.namelist():
            raise ValueError('文稿缺少样式信息，请先使用 Word 另存为 DOCX，或保留原格式导出')
        return parse_xml(z.read('word/document.xml')), parse_xml(z.read('word/styles.xml'))


def inherited_numbering(styles, sid):
    by_id = {s.get(qn('w:styleId')):s for s in styles.findall(qn('w:style'))}
    seen = set()
    while sid in by_id and sid not in seen:
        seen.add(sid)
        style = by_id[sid]
        props = style.find(qn('w:pPr'))
        numbering = props.find(qn('w:numPr')) if props is not None else None
        if numbering is not None:
            return deepcopy(numbering)
        parent = style.find(qn('w:basedOn'))
        sid = parent.get(qn('w:val')) if parent is not None else None
    return None


def structure(source):
    raw = source.read_bytes()
    doc, styles = read_package(io.BytesIO(raw))
    names = {s.get(qn('w:styleId')): s.find(qn('w:name')).get(qn('w:val'), '')
             for s in styles.findall(qn('w:style')) if s.find(qn('w:name')) is not None}
    rows, in_references, in_abstract = [], False, False
    for index, node in enumerate(doc.find(qn('w:body'))):
        if node.tag == qn('w:sectPr'):
            continue
        text = text_of(node).strip()
        is_p = node.tag == qn('w:p')
        style = node.find('w:pPr/w:pStyle', node.nsmap) if is_p else None
        sid = style.get(qn('w:val'), '') if style is not None else ''
        name = names.get(sid, sid).casefold().replace(' ', '').replace('_', '')
        role, inferred = 'body', False
        heading = re.fullmatch(r'(?:heading|标题|h)([123])', name)
        if not is_p or not text:
            role = 'keep'
        elif text.casefold() in ('references', 'bibliography', '参考文献'):
            role, in_references, in_abstract = 'reference_heading', True, False
        elif name in ('title', 'papertitle', '标题'):
            role = 'title'
        elif name in ('authors', 'author', 'au'):
            role = 'authors'
        elif name in ('affiliation', 'pi', 'pinospace'):
            role = 'affiliation'
        elif re.match(r'^(abstract|摘要)(?:\b|\s|[:：])', text, re.I) or name == 'abstract':
            role, in_abstract = 'abstract', True
        elif re.match(r'^(index terms|keywords|关键词)(?:\b|\s|[:：])', text, re.I) or name in ('it','keywords'):
            role, in_abstract = 'keywords', False
        elif name.startswith('aubios') or name == 'biography':
            role, in_references = 'biography', False
        elif heading or name.startswith('h1list') or name.startswith('h2'):
            role = 'heading' + (heading.group(1) if heading else '1' if name.startswith('h1') else '2')
            in_references, in_abstract = False, False
        elif name in ('figurecaption', 'caption') or re.match(r'^(?:Fig\.|Figure)\s+\d+', text):
            role = 'caption'
        elif name == 'tabletitle' or re.match(r'^TABLE\s+[IVX\d]+\b', text):
            role = 'table_caption'
        elif in_references or name in ('references', 'bibliography'):
            role = 'reference'
        elif in_abstract:
            role = 'abstract'
        elif not any(row['text'] for row in rows) and len(text) < 250:
            role, inferred = 'title', True
        rows.append({'id':f'b{index+1:05d}', 'position':index+1, 'kind':'paragraph' if is_p else 'table' if node.tag == qn('w:tbl') else 'object',
                     'text':text[:600], 'role':role, 'inferred':inferred, 'style':names.get(sid, sid)})
    # Untyped material before an explicit abstract is front matter, never guessed as author names.
    abstract = next((i for i, row in enumerate(rows) if row['role'] == 'abstract'), 0)
    for row in rows[:abstract]:
        if row['role'] == 'body':
            row['role'], row['inferred'] = 'frontmatter', True
    return {'source_sha256':hashlib.sha256(raw).hexdigest(), 'blocks':rows, 'roles':ROLES}


def content_signature(doc):
    """Formatting can change; text, controls, objects, math and relationships cannot."""
    copy = deepcopy(doc)
    for node in list(copy.iter()):
        if node.tag in {qn('w:pPr'), qn('w:rPr'), qn('w:sectPr')} and node.getparent() is not None:
            node.getparent().remove(node)
    # The one inserted section-break paragraph carries no content.
    for node in list(copy.iter(qn('w:p'))):
        if not len(node) and not node.text:
            node.getparent().remove(node)
    return etree.tostring(copy, method='c14n')


def apply_access(source, output, overrides=None, expected=None):
    return apply_preset(source, output, 'ieee-access', overrides, expected)


def apply_preset(source, output, template, overrides=None, expected=None):
    if template not in TEMPLATES:
        raise ValueError('请选择可用的 DOCX 排版模板')
    rules = TEMPLATES[template]
    label = 'IEEE Access' if template == 'ieee-access' else 'JCST'
    data = structure(source)
    if expected and expected != data['source_sha256']:
        raise ValueError('文稿已变化，请重新打开排版面板并检查文档结构')
    overrides = overrides or {}
    ids = {row['id'] for row in data['blocks'] if row['kind'] == 'paragraph'}
    if set(overrides) - ids or any(role not in ROLES for role in overrides.values()):
        raise ValueError('文档结构修正包含无效段落或类型')
    for row in data['blocks']:
        if row['id'] in overrides:
            row['role'], row['inferred'] = overrides[row['id']], False
    doc, styles = read_package(source)
    before = content_signature(doc)
    body = doc.find(qn('w:body'))
    nodes = list(body)
    issues = []

    def issue(code, message, row=None):
        issues.append({'code':code, 'message':message, 'block_id':row['id'] if row else None})

    # A namespaced style set avoids altering existing table, footnote or header styles.
    prefix = ('TMAccess' if template == 'ieee-access' else 'TMJCST') + data['source_sha256'][:10]
    while any(s.get(qn('w:styleId'), '').startswith(prefix) for s in styles):
        prefix += 'x'
    for role, rule in rules['rules'].items():
        style = OxmlElement('w:style', {qn('w:type'):'paragraph', qn('w:styleId'):prefix+role})
        name = OxmlElement('w:name', {qn('w:val'):'TransMux ' + label + ' ' + role})
        style.append(name)
        for tag, key in [('w:pPr','p'), ('w:rPr','r')]:
            props = OxmlElement(tag)
            for fragment in rule[key]:
                props.append(parse_xml(fragment.encode()))
            style.append(props)
        styles.append(style)
    paragraphs = [row for row in data['blocks'] if row['kind'] == 'paragraph']
    for row in paragraphs:
        node = nodes[row['position']-1]
        role = row['role']
        if row['inferred']:
            issue('inferred_structure', '自动推测的文档类型，请确认：' + ROLES[role], row)
        objects = node.xpath('.//w:drawing | .//w:pict | .//m:oMath | .//m:oMathPara')
        if objects:
            issue('complex_object', '本段含图片或公式，对象保持不变；请检查尺寸、行高、分栏和分页。', row)
        if role == 'keep':
            continue
        props = node.get_or_add_pPr()
        old_style = props.find(qn('w:pStyle'))
        if old_style is not None and props.find(qn('w:numPr')) is None:
            numbering = inherited_numbering(styles, old_style.get(qn('w:val')))
            if numbering is not None:
                props.get_or_add_numPr().getparent().replace(props.find(qn('w:numPr')), numbering)
        # Preserve numbering, section breaks, bookmarks and content controls.
        for child in list(props):
            if etree.QName(child).localname in ('pStyle','spacing','ind','jc','keepNext','keepLines','pageBreakBefore','outlineLvl','contextualSpacing'):
                props.remove(child)
        props.get_or_add_pStyle().val = prefix+role
        if role.startswith('heading') or role == 'reference_heading':
            props.get_or_add_keepNext().val = True
            props.get_or_add_outlineLvl().val = int(role[-1])-1 if role.startswith('heading') else 0
        # Keep bold/italic/superscripts, field codes, hyperlinks and equations intact.
        for run in node.iter(qn('w:r')):
            if run.find(qn('w:sym')) is not None:
                continue
            rpr = run.get_or_add_rPr()
            for child in list(rpr):
                if etree.QName(child).localname in ('rFonts','sz','szCs','spacing'):
                    rpr.remove(child)
            # Character styles can otherwise override the new paragraph font/size.
            for fragment in rules['rules'][role]['r']:
                prop = parse_xml(fragment.encode())
                tag = etree.QName(prop).localname
                if tag in ('rFonts','sz','szCs','spacing'):
                    rpr.insert_element_before(prop, *('w:'+key for key in RUN_ORDER[RUN_ORDER.index(tag)+1:]))
        if objects:
            # Exact line height can clip objects. Prefer preservation and flag manual review.
            props.get_or_add_spacing().set(qn('w:lineRule'), 'atLeast')
            props.get_or_add_spacing().set(qn('w:line'), '240')
        heading_number = r'^\d+(?:\.\d+)*\.?\s+' if template.startswith('jcst') else r'^(?:[IVXLCDM]+\.|[A-Z]\.|\d+[.)])\s'
        if role.startswith('heading') and not re.match(heading_number, row['text']):
            issue('heading_number', f'标题编号沿用原稿，请核对 {label} 的层级编号。', row)

    # Start double columns after the front matter. Never guess authors or add publisher placeholders.
    front = {'title','authors','affiliation','abstract','keywords','frontmatter'}
    boundary = next((row for row in data['blocks'] if row['role'] not in front | {'keep'}), None)
    if not boundary:
        raise ValueError('未识别到正文，请在文档结构中指定正文或一级标题后重试')
    cut = boundary['position']-1
    sections = list(doc.iter(qn('w:sectPr')))
    if not sections:
        raise ValueError('文稿缺少有效分节信息，请先使用 Word 另存为 DOCX')
    body_sections = []
    if len(sections) > 1:
        issue('existing_sections', '原稿有多个分节，已保留分节位置并应用版心；请检查跨栏对象和分页。')
    for section in sections:
        ancestor = section
        while ancestor.getparent() is not body:
            ancestor = ancestor.getparent()
        is_front = nodes.index(ancestor) < cut
        if not is_front:
            body_sections.append(section)
        for tag in ('pgSz','pgMar','cols','docGrid'):
            old = section.find(qn('w:'+tag))
            if old is not None:
                section.remove(old)
        # CT_SectPr insertion helpers keep OOXML element order valid.
        for fragment in rules['sections'][0 if is_front else 1]:
            prop = parse_xml(fragment.encode())
            insert_section_property(section, prop)
    if cut > 0:
        section = deepcopy(sections[0])
        for child in list(section):
            if etree.QName(child).localname not in ('headerReference','footerReference'):
                section.remove(child)
        for fragment in rules['sections'][0]:
            prop = parse_xml(fragment.encode())
            insert_section_property(section, prop)
        # Change the section following the front matter to a continuous break.
        body_sections[0].get_or_add_type().set(qn('w:val'), 'continuous')
        p = OxmlElement('w:p')
        p.get_or_add_pPr().append(section)
        body.insert(cut, p)
    required = {'title':'标题', 'authors':'作者', 'affiliation':'作者机构', 'abstract':'摘要', 'keywords':'关键词',
                'reference_heading':'参考文献部分', 'biography':'作者简介'}
    if template == 'jcst-submit':
        required.pop('biography')
    present = {row['role'] for row in data['blocks']}
    for role, label in required.items():
        if role not in present:
            issue('missing_'+role, '未识别到'+label+'，请补充内容或修正文档结构。')
    for row in data['blocks']:
        if row['kind'] != 'paragraph':
            issue('complex_block', '保留原对象结构与尺寸，请检查在双栏中的宽度、位置与题注。', row)
    # Bibliographic metadata and author-year conversion are a later, separate workflow.
    issue('references_pending', '引文与文献条目内容、编号保持原样；尚未自动转换作者年份制或核验文献信息。')
    if content_signature(doc) != before:
        raise ValueError('内容完整性检查失败，未发布排版结果')
    with ZipFile(source) as src, ZipFile(output, 'w') as dst:
        for item in src.infolist():
            content = etree.tostring(doc if item.filename == 'word/document.xml' else styles, xml_declaration=True,
                                     encoding='UTF-8', standalone=True) if item.filename in ('word/document.xml','word/styles.xml') else src.read(item.filename)
            dst.writestr(item, content)
        if any(re.match(r'word/(?:header|footer|footnotes|endnotes)', name) for name in src.namelist()):
            issue('ancillary_parts', '原稿的页眉、页脚及脚注样式保持原样，请核对它们与投稿版式的兼容性。')
    return {'blocks':data['blocks'], 'findings':issues, 'checks':[
        {'id':'content_preserved', 'passed':True, 'message':'正文、公式、图片、表格和引用内容保持不变；资源文件原样保留。'},
        {'id':'template_rules', 'passed':True, 'message':'已应用官方模板的版心、双栏及段落样式；仍需人工检查期刊格式细节。'}]}
