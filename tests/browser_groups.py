"""Read-only UI acceptance against an isolated real-agent acceptance workspace."""
import argparse
import json
from pathlib import Path
import socket
import threading
import time

import httpx
from playwright.sync_api import sync_playwright
import uvicorn

from transmux.app import create_app


def main(root):
    report = json.loads((root / 'acceptance.json').read_text())
    fid = Path(report['translation']).stem
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    server = uvicorn.Server(uvicorn.Config(create_app(root), host='127.0.0.1', port=port, log_level='error'))
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1050})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base)
            page.locator(f'[data-compare="{fid}"]').click()
            page.locator('.comparison-pair').first.wait_for()
            assert page.locator('.comparison-pair').count() == report['groups']
            assert f"原文 {report['source_paragraphs']} 段 → 译文 {report['target_paragraphs']} 段" in page.locator('#comparison-count').inner_text()
            merged = page.locator('#compare-paragraph-2')
            assert merged.locator('.comparison-original .comparison-text').count() == 2
            assert merged.locator('.comparison-translation .comparison-text').count() == 1
            split = page.locator('#compare-paragraph-3')
            assert split.locator('.comparison-original .comparison-text').count() == 1
            assert split.locator('.comparison-translation .comparison-text').count() == 2
            assert merged.locator('.comparison-group-reason').is_visible()
            page.locator('#comparison-dialog').screenshot(path=str(root / 'comparison-desktop.png'))
            page.locator('#comparison-view-latest').click()
            page.wait_for_function("document.querySelectorAll('#compare-paragraph-2 .comparison-translation .comparison-text').length === 2")
            page.locator('[data-revise-paragraph="2"]').click()
            assert '恢复原文分段' in page.locator('[data-revision-form="2"]').inner_text()
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.locator('#comparison-dialog').evaluate('el => el.scrollWidth <= el.clientWidth')
            page.locator('#comparison-dialog').screenshot(path=str(root / 'comparison-mobile.png'))
            assert not errors, errors
            browser.close()
            print('Grouped comparison, merge/split counts, revision version, desktop/mobile: passed')
    finally:
        server.should_exit = True
        thread.join(timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    main(parser.parse_args().root)
