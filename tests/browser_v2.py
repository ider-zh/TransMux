"""Browser acceptance for the three-column conversation workspace."""
import io
import asyncio
import tempfile
import threading
import time
from unittest.mock import patch

import httpx
import uvicorn
from docx import Document
from starlette.responses import JSONResponse
from playwright.sync_api import sync_playwright

from transmux.v2 import create_v2_app, WorkspaceWorker
from test_glossary_documents import GlossaryRunner


async def available_models(agent):
    await asyncio.sleep(.6 if agent == 'codex' else .05)
    return [{'id': agent + '-fresh-model', 'name': agent + ' latest', 'default': True}]


def main():
    with tempfile.TemporaryDirectory(prefix='transmux-v2-browser-') as root, patch('transmux.model_catalog.live_models', available_models), patch('transmux.app.availability', lambda: [{'id': 'codex', 'available': True}, {'id': 'codebuddy', 'available': True}]):
        def factory(store):
            runner = GlossaryRunner()
            runner.store = store
            return WorkspaceWorker(store, runner)
        app = create_v2_app(root, factory)
        app.state.delay_upload = False
        @app.middleware('http')
        async def delay_upload(request, call_next):
            if request.url.path.endswith('/attachments') and app.state.delay_upload:
                await request.body()
                while app.state.delay_upload:
                    await asyncio.sleep(.05)
                return JSONResponse({'detail': '文件过大'}, status_code=413)
            return await call_next(request)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=18766, log_level='error'))
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            for _ in range(100):
                try:
                    if httpx.get('http://127.0.0.1:18766/api/health', trust_env=False).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(.1)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1500, 'height': 980})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto('http://127.0.0.1:18766')
                page.locator('#newWorkspace').click()
                page.locator('[name=name]').fill('Research · English')
                page.locator('#createAgent').select_option('codebuddy')
                page.wait_for_function("document.querySelector('#createModel').value === 'codebuddy-fresh-model'")
                page.wait_for_timeout(700)
                assert page.locator('#createModel option[value=codex-fresh-model]').count() == 0
                page.locator('#createAgent').select_option('codex')
                page.wait_for_function("document.querySelector('#createModel').value === 'codex-fresh-model'")
                page.locator('#refreshCreateModels').click()
                page.wait_for_function("!document.querySelector('#createModel').disabled")
                page.locator('#createForm .primary').click()
                page.wait_for_function("document.querySelector('#workspaceName').textContent==='Research · English'")
                page.locator('#referenceHint').wait_for(state='visible')
                page.wait_for_function("document.querySelector('#model').value === 'codex-fresh-model'")
                doc = Document()
                doc.add_paragraph('Paris is in Germany.')
                stream = io.BytesIO()
                doc.save(stream)
                page.locator('#fileInput').set_input_files({'name': 'paper.docx', 'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'buffer': stream.getvalue()})
                page.locator('.attachment').wait_for()
                page.locator('[data-kind=translate]').click()
                assert '翻译风格' in page.locator('#prompt').input_value()
                page.locator('#send').click()
                page.locator('#feed .artifact').first.wait_for(timeout=30000)
                assert page.locator('.work-status[open]').count() == 0
                page.locator('#feed .artifact').first.click()
                page.locator('#preview .doc-paragraph').wait_for()
                assert 'Paris is in Germany.' in page.locator('#previewBody').inner_text()
                assert page.locator('#preview').bounding_box()['width'] > 500
                page.locator('#closePreview').click()
                page.locator('[data-review-approve]').first.click()
                page.wait_for_function("document.querySelector('#toast').textContent.includes('人工审核通过')")
                page.locator('[data-kind=factcheck]').click()
                assert '最新已审核译文' in page.locator('#scope').inner_text()
                page.locator('#send').click()
                page.locator('[data-accept]').wait_for(timeout=30000)
                page.locator('[data-accept]').click()
                page.wait_for_function("document.querySelector('#feed').textContent.includes('修订副本与修改说明已生成')")
                page.locator('#tree [data-config=requirements]').click()
                page.locator('#sourceTab').click()
                page.locator('#configEditor').fill('Use concise English.')
                page.locator('#saveConfig').click()
                page.wait_for_function("document.querySelector('#toast').textContent.includes('已保存')")
                page.locator('#historyButton').click()
                page.locator('#historyDialog').wait_for(state='visible')
                assert 'Use concise English.' in page.locator('#historyList').text_content()
                page.locator('#historyDialog [data-close]').click()
                page.locator('#tree [data-config=mappings]').click()
                page.locator('#addRow').click()
                page.locator('[data-field=original]').fill('人工智能')
                page.locator('[data-field=translation]').fill('artificial intelligence')
                page.locator('[data-field=context]').fill('Technical prose')
                page.locator('#saveConfig').click()
                page.wait_for_timeout(500)
                assert '已保存' in page.locator('#toast').inner_text()
                page.locator('#closePreview').click()
                assert page.locator('#composer').bounding_box()['y'] + page.locator('#composer').bounding_box()['height'] <= 980
                assert page.evaluate('document.documentElement.scrollHeight <= innerHeight')
                page.screenshot(path='/tmp/transmux-v2-desktop.png', full_page=True)
                assert not errors, errors
                page.set_viewport_size({'width': 780, 'height': 900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path='/tmp/transmux-v2-tablet.png', full_page=True)
                page.set_viewport_size({'width': 1500, 'height': 980})
                # Two independently approved styles require explicit selection; edits target a version.
                pid = page.locator('[data-project].active').get_attribute('data-project')
                style_ids = []
                for name in ['reference-one.docx', 'reference-two.docx']:
                    page.locator('#fileInput').set_input_files({'name': name, 'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'buffer': stream.getvalue()})
                    page.locator('.attachment').wait_for()
                    page.locator('[data-kind=style]').click()
                    page.locator('#send').click()
                    page.wait_for_function("name => document.querySelector('#reviewCards')?.textContent.includes(name)", arg=name)
                    reviews = page.request.get(f'http://127.0.0.1:18766/api/projects/{pid}/reviews').json()
                    review = next(r for r in reviews if r['kind'] == 'style' and r['id'] not in style_ids)
                    style_ids.append(review['id'])
                    page.locator(f'[data-review-preview="{review["file_id"]}"]').click()
                    page.wait_for_function("document.querySelector('#previewName').textContent.includes('待审核草稿')")
                    page.locator('#closePreview').click()
                    page.locator(f'[data-review-approve="{review["id"]}"]').click()
                    page.locator(f'[data-review-approve="{review["id"]}"]').wait_for(state='detached')
                    page.locator('[data-kind=translate]').click()
                    if len(style_ids) == 1:
                        assert page.locator('#styleChoice').input_value() == review['id']
                        assert page.locator('#styleChoice').is_disabled()
                assert page.locator('#styleChoice').is_enabled()
                assert page.locator('#styleChoice option').count() == 3
                page.locator('#styleChoice').select_option(style_ids[0])
                assert page.locator('#styleChoice').input_value() == style_ids[0]
                page.locator(f'#tree [data-file="{review["file_id"]}"]').click()
                page.locator('#reviseVersion').click()
                assert '修改对象' in page.locator('#reviewTarget').inner_text()
                assert page.locator('#clearTask').is_hidden()  # revision uses ordinary chat
                page.locator('#closePreview').click()
                page.locator('[data-kind=translate]').click()
                assert page.locator('#glossaryChoiceLabel').is_hidden()
                glossary_ids = []
                for name in ['terms-one.txt', 'terms-two.txt']:
                    page.locator('#fileInput').set_input_files({'name': name, 'mimeType': 'text/plain', 'buffer': '自动机 | automaton | Computer science.'.encode()})
                    page.locator('.attachment').wait_for()
                    page.locator('[data-kind=glossary]').click()
                    page.locator('#send').click()
                    page.wait_for_function("name => document.querySelector('#reviewCards')?.textContent.includes(name)", arg=name)
                    reviews = page.request.get(f'http://127.0.0.1:18766/api/projects/{pid}/reviews').json()
                    glossary = next(r for r in reviews if r['kind'] == 'glossary' and r['id'] not in glossary_ids)
                    glossary_ids.append(glossary['id'])
                    page.locator(f'[data-review-preview="{glossary["file_id"]}"]').click()
                    page.locator('#previewBody .glossary-table').wait_for()
                    assert 'automaton' in page.locator('#previewBody').inner_text()
                    page.locator('#sourceTab').click()
                    assert '| 自动机 | automaton |' in page.locator('#previewBody').inner_text()
                    page.locator('#closePreview').click()
                    page.locator(f'[data-review-approve="{glossary["id"]}"]').click()
                    page.locator(f'[data-review-approve="{glossary["id"]}"]').wait_for(state='detached')
                    page.locator('[data-kind=translate]').click()
                    assert page.locator('#glossaryChoiceLabel').is_visible()
                    if len(glossary_ids) == 1:
                        assert page.locator('#glossaryChoice').is_disabled()
                        assert page.locator('#glossaryChoice').input_value() == glossary['id']
                assert page.locator('#glossaryChoice').is_enabled()
                assert page.locator('#glossaryChoice option').count() == 3
                page.locator('#glossaryChoice').select_option(glossary_ids[0])
                assert page.locator('#glossaryChoice').input_value() == glossary_ids[0]
                # A slow model catalog must not block project contents or retain old DOM.
                page.set_viewport_size({'width': 1500, 'height': 980})
                first = page.locator('[data-project].active').get_attribute('data-project')
                second = page.request.post('http://127.0.0.1:18766/api/projects', data={'name': 'Empty workspace', 'agent': 'codebuddy', 'target_language': 'en'}).json()['id']
                page.reload()
                page.locator(f'[data-project="{second}"]').wait_for()
                held = []
                page.route('**/api/agents/codebuddy/models', lambda route: held.append(route))
                page.locator(f'[data-project="{second}"]').click()
                page.wait_for_function("document.querySelector('#workspaceName').textContent === 'Empty workspace'")
                page.wait_for_function("document.querySelector('#feed').textContent.includes('从一份文档开始') || !document.querySelector('#feed').textContent.includes('正在加载')")
                assert 'Paris' not in page.locator('#previewBody').inner_text()
                assert 'paper.docx' not in page.locator('#tree').inner_text()
                assert page.locator('.attachment').count() == 0
                assert held, 'model request should still be pending'
                held.pop().fulfill(json={'models': ['buddy-test']})
                # Hold the upload response after transfer; show parsing, and isolate on switch.
                app.state.delay_upload = True
                page.locator('#fileInput').set_input_files({'name': 'slow.txt', 'mimeType': 'text/plain', 'buffer': b'Slow document'})
                page.wait_for_function("document.querySelector('#uploadProgress').textContent.includes('正在解析')")
                assert page.locator('#uploadProgress progress').get_attribute('value') is None
                page.locator(f'[data-project="{first}"]').click()
                page.wait_for_function("document.querySelector('#workspaceName').textContent === 'Research · English'")
                assert page.locator('#uploadProgress').is_hidden()
                assert page.locator('#send').is_enabled()
                app.state.delay_upload = False
                page.locator(f'[data-project="{second}"]').click()
                page.wait_for_function("document.querySelector('#uploadProgress').textContent.includes('文件过大')")
                assert page.locator('#send').is_enabled()
                if held:
                    held.pop().fulfill(json={'models': []})
                assert not errors, errors
                browser.close()
                print('V2 browser acceptance passed: upload, scoped translation, preview, fact report, confirmation, history, terminology table, responsive layout.')
        finally:
            server.should_exit = True
            thread.join(timeout=15)


if __name__ == '__main__':
    main()
