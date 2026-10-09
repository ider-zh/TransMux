"""Extract formatting from official JCST Submit .doc after LibreOffice DOCX conversion."""
import hashlib
import json
from pathlib import Path
import sys
import extract_jcst_template as extractor

extractor.SAMPLES = {'title':(0,'Journal of Computer Science',16,True,False),'abstract':(2,'Abstract',12,False,False),'keywords':(3,'Keywords',12,False,False),'body':(6,'Journal of Computer Science',12,False,False),'heading1':(5,'Introduction',12,True,False),'heading2':(13,'Text',12,True,False),'heading3':(23,'2.2.1',12,False,True),'reference_heading':(135,'References',12,True,False),'reference':(136,'[1]',12,False,False),'table_caption':(38,'Table 1',10,False,False)}
source, converted, destination = map(Path, sys.argv[1:])
extractor.extract(converted, destination)
data = json.loads(destination.read_text())
publish = json.loads((destination.parent / 'jcst.json').read_text())
for role in ('authors','affiliation','caption','biography','frontmatter'):
    data['rules'][role] = publish['rules'][role]
data.update(id='jcst-submit', name='JCST · 投稿排版草稿', version='2022.09-submit-r1', verified_on='2026-10-08', source_url='https://wqketang.cn-beijing.oss.aliyuncs.com/office/journal_prod/2022-09-27/e2409c6e-fdf5-4afc-bca9-5f4536e6c7da.doc', source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), converted_sha256=hashlib.sha256(converted.read_bytes()).hexdigest(), notice='投稿版主体样式来自官方 Submit Word 模板；作者、机构、图题等附加角色沿用已核实的 Publish 样式，保留输入内容。')
data['citations'] = publish['citations']
destination.write_text(json.dumps(data,ensure_ascii=False,indent=2))
