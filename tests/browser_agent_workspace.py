"""Browser acceptance for Agent libraries, task options and reviewed outputs."""
import asyncio
import io
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

import httpx
import uvicorn
from docx import Document
from playwright.sync_api import sync_playwright
from starlette.responses import JSONResponse

from transmux.v2 import create_v2_app, WorkspaceWorker
from transmux.usage import UsageCall
from test_agent_workspace import ConsistencyRunner


async def models(agent):
    await asyncio.sleep(.4 if agent == 'codex' else .05)
    return [{'id': agent + '-first', 'name': 'First', 'default': False},
            {'id': agent + '-default', 'name': 'Default', 'default': True}]


class MeteredRunner(ConsistencyRunner):
    async def run_isolated(self, pid, jid, prompt, schema=None):
        await asyncio.sleep(3 if schema and 'translations' in schema['properties'] else .1)
        call = UsageCall(self.store, pid, jid, 'codex')
        result = await super().run_isolated(pid, jid, prompt, schema)
        call.consume({'type': 'turn.completed', 'usage': {'input_tokens': 1000, 'cached_input_tokens': 200, 'output_tokens': 100}})
        return result


def main():
    with tempfile.TemporaryDirectory(prefix='transmux-agent-browser-') as root, \
            patch('transmux.model_catalog.live_models', models), \
            patch('transmux.app.availability', lambda: [{'id': a, 'available': True} for a in ('codex', 'codebuddy')]):
        def factory(store):
            runner = MeteredRunner()
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
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=18767, log_level='error'))
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            for _ in range(100):
                try:
                    if httpx.get('http://127.0.0.1:18767/api/health', trust_env=False).is_success:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(.1)
            with sync_playwright() as pw:
                candidates = [Path('/usr/bin/google-chrome'), *sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux/chrome'))]
                executable = os.getenv('CHROME_BIN') or next((str(p) for p in candidates if p.exists()), None)
                browser = pw.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1500, 'height': 980})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:18767')
                page.locator('#newWorkspace').click()
                assert page.locator('[name=name]').input_value() == 'Codex · 英译助手'
                page.locator('#createAgent').select_option('codebuddy')
                page.wait_for_function("document.querySelector('#createModel').value === 'codebuddy-first'")
                page.wait_for_timeout(500)
                assert not page.locator('#createModel option[value=codex-first]').count()
                assert page.locator('[name=name]').input_value() == 'CodeBuddy · 英译助手'
                page.locator('[name=name]').fill('Research Agent')
                page.locator('#createAgent').select_option('codex')
                page.wait_for_function("document.querySelector('#createModel').value === 'codex-first'")
                assert page.locator('[name=name]').input_value() == 'Research Agent'
                page.locator('#createForm .primary').click()
                page.wait_for_function("document.querySelector('#workspaceName').textContent === 'Research Agent'")
                pid = page.locator('[data-project].active').get_attribute('data-project')
                assert page.locator('#templates button').all_text_contents() == ['学习 ↗', '翻译 ↗', '排版 ↗', '一致性检查 ↗', '上传专有名词表 ↗']
                assert page.locator(f'[data-agent="{pid}"] #agentLibrary').is_visible()
                assert page.locator('#composer').bounding_box()['y'] > page.locator('#feed').bounding_box()['y']
                assert page.locator('#composer').bounding_box()['y'] + page.locator('#composer').bounding_box()['height'] <= 980
                assert not page.locator('#tree').is_visible()
                page.locator('[data-library=documents]').click()
                assert page.locator('#tree').is_visible()
                page.locator('#libraryDialog [data-close]').click()
                assert page.locator('#attach').bounding_box()['y'] < page.locator('#templates').bounding_box()['y']

                def upload(name, text):
                    doc = Document(); doc.add_paragraph(text)
                    stream = io.BytesIO(); doc.save(stream)
                    page.locator('#fileInput').set_input_files({'name': name, 'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'buffer': stream.getvalue()})
                    page.wait_for_function("name => document.querySelector('#attachments').textContent.includes(name) && !document.querySelector('#send').disabled", arg=name)

                def task(kind):
                    page.locator(f'[data-kind={kind}]').click()
                    if kind in ('translate', 'layout', 'consistency'):
                        page.locator('#confirmTaskOptions').click()

                def latest(kind):
                    page.wait_for_function("kind => !!document.querySelector(`[data-output-kind=${kind}] .review-card`)", arg=kind)
                    rows = page.request.get(f'http://127.0.0.1:18767/api/projects/{pid}/reviews').json()
                    return next(r for r in rows if r['kind'] == kind and r['status'] == 'pending')

                def approve(row):
                    page.locator(f'[data-review-approve="{row["id"]}"]').click()
                    if row['kind'] == 'translation':
                        page.locator('#approveTranslation').wait_for(state='visible')
                        assert 'Paris is in France.' in page.locator('.comparison-source').inner_text()
                        assert 'Paris is in Germany.' in page.locator('.comparison-target').inner_text()
                        page.locator('#approveTranslation').click()
                    if row['kind'] == 'style':
                        page.locator('#styleRulesDialog').wait_for(state='visible')
                        assert page.locator('[data-style-rule]').first.is_checked()
                        page.locator('#approveStyleRules').click()
                    page.locator(f'[data-review-approve="{row["id"]}"]').wait_for(state='detached')

                page.locator('[data-kind=style]').click()
                assert '需要文档' in page.locator('#taskWarning').inner_text()
                assert page.request.get(f'http://127.0.0.1:18767/api/projects/{pid}/jobs').json() == []
                upload('paper.docx', 'Paris is in France.')
                assert 'paper.docx' in page.locator('#tree').inner_text()
                assert 'paper.docx' not in page.locator('#outputs').inner_text()
                task('translate')
                page.wait_for_function("document.querySelector('#agentStatus').textContent.includes('翻译中')")
                assert page.locator('#agentStatus small').inner_text()
                translation = latest('translation')
                page.wait_for_function("document.querySelector('#agentStatus').textContent.includes('待审核')")
                assert page.locator(f'[data-history-review="{translation["id"]}"].pending').is_visible()
                page.locator(f'#outputs [data-review-preview="{translation["file_id"]}"]').click()
                page.locator('#preview .comparison-target .doc-paragraph').wait_for()
                assert 'Paris is in France.' in page.locator('.comparison-source').inner_text()
                assert 'Paris is in Germany.' in page.locator('#previewBody').inner_text()
                page.locator('#closePreview').click()
                comparison_url = f'**/files/{translation["file_id"]}/comparison'
                page.route(comparison_url, lambda route: route.fulfill(status=400, content_type='application/json', body='{"detail":"缺少段落对应记录"}'))
                page.locator(f'[data-review-approve="{translation["id"]}"]').click()
                page.locator('#previewBody [role=alert]').wait_for()
                assert page.locator('#approveTranslation').is_disabled()
                page.locator('#closePreview').click()
                page.unroute(comparison_url)
                approve(translation)
                assert page.locator(f'[data-output="{translation["file_id"]}"]').is_visible()
                assert page.locator('#attachments .attachment').count() == 0
                page.locator(f'#outputs [data-review-attach="{translation["file_id"]}"]').click()
                task('consistency')
                report = latest('consistency')
                page.locator(f'#outputs [data-review-preview="{report["file_id"]}"]').click()
                page.wait_for_function("document.querySelector('#previewBody').textContent.includes('Paris is in France.')")
                assert 'Paris is in Germany.' in page.locator('#previewBody').inner_text()
                page.locator('#closePreview').click()
                page.locator(f'#feed [data-review-ignore="{report["id"]}"]').click()
                page.wait_for_function('''id => document.querySelector(`[data-history-review="${id}"]`)?.classList.contains('ignored')''', arg=report['id'])
                assert page.locator(f'[data-output-status=pending] [data-output="{report["file_id"]}"]').count() == 0
                page.reload()
                page.locator(f'#feed [data-review-restore="{report["id"]}"]').wait_for()
                page.locator(f'#feed [data-review-restore="{report["id"]}"]').click()
                page.locator(f'#feed [data-review-approve="{report["id"]}"]').wait_for()
                approve(report)

                page.locator('[data-kind=layout]').click()
                assert '需要文档' in page.locator('#taskWarning').inner_text()
                page.locator(f'#feed [data-review-attach="{translation["file_id"]}"]').click()
                page.locator('[data-kind=layout]').click()
                assert page.locator('#presetLabel').is_visible()
                page.locator('#preset').select_option('jcst')
                page.locator('#cancelTaskOptions').click()
                assert page.locator('#preset').input_value() == 'original'
                page.locator('#taskOptions').click()
                page.locator('#preset').select_option('original')
                page.locator('#confirmTaskOptions').click()
                layout = latest('layout'); approve(layout)

                upload('reference.docx', 'Academic English is concise and precise. Use clear sentences and consistent terminology.')
                task('style')
                style = latest('style'); approve(style)
                assert 'reference.docx' in page.locator('#resources').inner_text()
                upload('terms.docx', '自动机 | automaton | Computer science.')
                task('glossary')
                glossary = latest('glossary'); approve(glossary)
                page.locator('#pickFiles').click()
                page.locator('#fileChoices input').first.check()
                page.locator('#filesDialog [data-close]').click()
                page.locator('[data-kind=translate]').click()
                assert page.locator('#styleChoice').input_value() == style['id']
                assert page.locator('#glossaryChoice').input_value() == glossary['id']
                page.locator('#confirmTaskOptions').click()
                page.locator('[data-library=resources]').click()
                page.locator('#resources [data-config=requirements]').click()
                page.locator('#libraryDialog').evaluate('el => el.close()')
                page.locator('#sourceTab').click()
                page.locator('#configEditor').fill('Use concise English.')
                page.locator('#saveConfig').click()
                page.wait_for_function("document.querySelector('#toast').textContent.includes('已保存')")
                page.locator('#historyButton').click()
                page.locator('#historyDialog').wait_for(state='visible')
                assert 'Use concise English.' in page.locator('#historyList').text_content()
                page.locator('#historyDialog [data-close]').click()
                page.locator('#closePreview').click()

                page.reload()
                page.locator(f'[data-output="{report["file_id"]}"]').wait_for()
                assert page.locator('#feed .history-review').count() > 0
                assert page.locator('.workspace [data-config]').count() == 0
                assert page.locator('#feed .task-usage strong').count() > 0
                assert page.locator('#feed .message.user').first.evaluate("el => getComputedStyle(el).backgroundColor") == 'rgba(0, 0, 0, 0)'
                page.locator('#settings').click()
                page.wait_for_function("document.querySelector('#agentUsage').textContent.includes('Kimi K3')")
                usage = page.request.get(f'http://127.0.0.1:18767/api/projects/{pid}/usage').json()
                assert usage['total']['total_tokens'] > 0
                assert f"¥{usage['total']['estimated_cny']:.6f}" in page.locator('#agentUsage').inner_text()
                page.locator('#settingsDialog [data-close]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path='/tmp/transmux-agents-desktop.png', full_page=True)
                for width in (780, 390):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert page.locator('.workspace').is_visible()
                page.screenshot(path='/tmp/transmux-agents-mobile.png', full_page=True)
                page.set_viewport_size({'width': 1500, 'height': 980})
                second = page.request.post('http://127.0.0.1:18767/api/projects', data={'name': 'Separate Agent', 'agent': 'codebuddy'}).json()['id']
                page.reload()
                page.locator(f'[data-project="{second}"]').click()
                page.wait_for_function("document.querySelector('#workspaceName').textContent === 'Separate Agent'")
                page.wait_for_function("!document.querySelector('#feed').textContent.includes('正在加载')")
                assert page.locator(f'[data-agent="{second}"] #agentLibrary').is_visible()
                assert 'paper.docx' not in page.locator('#tree').inner_text()
                assert page.locator('#outputs .review-card').count() == 0
                app.state.delay_upload = True
                page.locator('#fileInput').set_input_files({'name': 'slow.txt', 'mimeType': 'text/plain', 'buffer': b'Slow document'})
                page.wait_for_function("document.querySelector('#uploadProgress').textContent.includes('正在解析')")
                assert page.locator('#uploadProgress progress').get_attribute('value') is None
                page.locator(f'[data-project="{pid}"]').click()
                page.wait_for_function("document.querySelector('#workspaceName').textContent === 'Research Agent'")
                assert page.locator('#uploadProgress').is_hidden()
                assert page.locator('#send').is_enabled()
                app.state.delay_upload = False
                page.locator(f'[data-project="{second}"]').click()
                page.wait_for_function("document.querySelector('#uploadProgress').textContent.includes('文件过大') && !document.querySelector('#send').disabled")
                assert not errors, errors
                browser.close()
                print('Agent workspace browser acceptance passed.')
        finally:
            server.should_exit = True
            thread.join(timeout=15)


if __name__ == '__main__':
    main()
