"""Full browser acceptance for terminology management with isolated data."""
import socket
import tempfile
import threading
import time

import httpx
from playwright.sync_api import sync_playwright
import uvicorn

from transmux.app import create_app
from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import Store
from test_workflows import FakeRunner, TinyEmbeddings


class ReviewRunner(FakeRunner):
    async def run(self, pid, jid, prompt, schema=None):
        if schema and 'decisions' in schema['properties']:
            return {'decisions': [dict(index=0,action='disable',reason='Only a definition, no consistency requirement.',
                                       scope='',meaning='',usage='',translation='',aliases='',context='')]}
        return await super().run(pid,jid,prompt,schema)


def main():
    with tempfile.TemporaryDirectory(prefix='term-ui-') as root:
        store = Store(root)
        project = store.create_project('术语测试', 'codex')
        pid = project['id']
        store.merge_terminology(pid, 'terms', [dict(term='Script',meaning='A representational concept',usage='Definition only',source='paper.txt')], 'extraction','fixture')
        store.db.close()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        app = create_app(root, lambda store: Worker(store, ReviewRunner(), Rag(TinyEmbeddings())))
        server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            with httpx.Client(trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get(base + '/api/health').status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.1)
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
                page = browser.new_page(viewport=dict(width=1440,height=1050))
                errors = []
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base)
                page.locator('#terms-table textarea').first.wait_for()
                style = page.locator('#style-editor').bounding_box()
                table = page.locator('#terminology-card').bounding_box()
                assert table['y'] > style['y'] + style['height']
                assert table['width'] > 650
                page.locator('#expand-terminology').click()
                assert page.locator('#terms-expanded').is_visible()
                page.locator('#rescreen-terms').click()
                page.wait_for_function("state.jobs.some(j => j.kind === 'terminology_review' && j.state === 'succeeded')")
                page.locator('#preview-rescreen').click()
                page.locator('#term-review-dialog').wait_for(state='visible')
                assert '停用' in page.locator('#term-review-items').inner_text()
                assert page.evaluate("JSON.parse(document.querySelector('#terms-editor').value).rows[0].status || 'active'") == 'active'
                page.locator('#term-review-apply').click()
                page.locator('#term-review-dialog').wait_for(state='hidden')
                assert page.locator('.term-state.inactive').inner_text() == '已停用'
                page.locator('#tab-people').click()
                page.locator('[data-import-terms="people"]').click()
                page.locator('#term-import-paste').fill('原文\t译文\t语境\n马文·明斯基\tMarvin Minsky\tAI research\n王海青\t\tThis experiment')
                page.locator('#term-import-parse').click()
                page.locator('#term-import-preview').wait_for(state='visible')
                page.locator('#term-import-preview').click()
                page.locator('#term-import-apply').wait_for(state='visible')
                assert page.locator('#term-import-rows .term-preview-item').count() == 2
                assert page.evaluate("JSON.parse(document.querySelector('#people-editor').value).rows.length") == 0
                page.locator('#term-import-apply').click()
                page.locator('#term-import-dialog').wait_for(state='hidden')
                assert page.locator('#people-table .term-state.pending').count() == 1
                page.locator('#term-filter').select_option('pending')
                assert page.locator('#people-table [data-field="original"]').input_value() == '王海青'
                page.locator('#people-table [data-field="translation"]').fill('Wang Haiqing')
                page.wait_for_function("document.querySelector('#people-status').textContent === '已同步'")
                page.locator('#people-table [data-term-resolve="activate"]').click()
                page.wait_for_function("JSON.parse(document.querySelector('#people-editor').value).rows.every(r => r.status === 'active')")
                page.locator('#term-filter').select_option('all')
                # Import a changed spelling: conflict must default to skip and show the old value.
                page.locator('[data-import-terms="people"]').click()
                page.locator('#term-import-paste').fill('原文\t译文\t语境\n马文·明斯基\tDifferent Name\tAI research')
                page.locator('#term-import-parse').click()
                page.locator('#term-import-preview').click()
                page.locator('#term-import-apply').wait_for(state='visible')
                assert page.locator('[data-import-choice="0"]').input_value() == 'skip'
                assert 'Marvin Minsky' in page.locator('#term-import-rows').inner_text()
                page.locator('#term-import-apply').click()
                page.locator('#term-import-dialog').wait_for(state='hidden')
                assert page.locator('#people-table [data-field="translation"]').first.input_value() == 'Marvin Minsky'
                page.locator('#terms-expanded').screenshot(path='/tmp/transmux-terminology-desktop.png')
                page.set_viewport_size(dict(width=390,height=844))
                assert page.locator('#terms-expanded').evaluate('el => el.scrollWidth <= el.clientWidth')
                page.locator('#terms-expanded').screenshot(path='/tmp/transmux-terminology-mobile.png')
                page.locator('#close-terms-expanded').click()
                page.reload()
                page.locator('#tab-people').click()
                page.wait_for_function("document.querySelector('#people-editor').value.includes('Marvin Minsky')")
                assert not errors, errors
                browser.close()
                print('Terminology browser passed: full width, expansion, rescreen preview/apply, paste import, conflict skip, people confirmation, persistence and mobile.')
        finally:
            server.should_exit = True
            thread.join(timeout=15)


if __name__ == '__main__':
    main()
