"""Read-only CLI model discovery; never creates a conversation or runs a prompt."""
import asyncio
import contextlib
import json
import os
import signal
import tempfile
import time

from .agents import agent_environment, model_choices


async def live_models(agent):
    binary = os.getenv('TRANSMUX_' + agent.upper() + '_BIN', agent)
    args = [binary, 'app-server', '--listen', 'stdio://'] if agent == 'codex' else [
        binary, '-p', '--verbose', '--input-format', 'stream-json', '--output-format', 'stream-json',
        '--permission-mode', 'dontAsk', '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--no-session-persistence']
    process = None
    with tempfile.TemporaryDirectory(prefix='transmux-models-') as directory:
        try:
            async with asyncio.timeout(20):
                process = await asyncio.create_subprocess_exec(*args, cwd=directory, env=agent_environment(agent),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=4 * 1024 * 1024)
                async def send(value):
                    process.stdin.write((json.dumps(value) + '\n').encode())
                    await process.stdin.drain()

                async def receive(identifier):
                    while line := await process.stdout.readline():
                        try:
                            value = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(value, dict):
                            continue
                        if agent == 'codex' and value.get('id') == identifier:
                            if 'error' in value:
                                raise ValueError('Agent 拒绝模型查询')
                            return value['result']
                        response = value.get('response', {})
                        if agent == 'codebuddy' and value.get('type') == 'control_response' and response.get('request_id') == identifier:
                            if response.get('subtype') == 'error':
                                raise ValueError('Agent 拒绝模型查询')
                            return response.get('response', {})
                    raise ValueError('Agent 未返回模型列表')

                if agent == 'codex':
                    await send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'transmux_models', 'version': '0.2.0'}, 'capabilities': {'experimentalApi': False}}})
                    await receive(1)
                    await send({'method': 'initialized', 'params': {}})
                    result, cursor = [], None
                    for number in range(2, 102):
                        await send({'id': number, 'method': 'model/list', 'params': {'includeHidden': False, 'limit': 100, 'cursor': cursor}})
                        page = await receive(number)
                        result.extend({'id': m['model'], 'name': m.get('displayName', m['model']), 'default': m.get('isDefault', False)} for m in page['data'])
                        cursor = page.get('nextCursor')
                        if not cursor:
                            return result
                    raise ValueError('模型列表分页超出限制')
                await send({'type': 'control_request', 'request_id': 'init', 'request': {'subtype': 'initialize', 'hasPrompt': False}})
                await receive('init')
                await send({'type': 'control_request', 'request_id': 'models', 'request': {'subtype': 'get_available_models'}})
                result = await receive('models')
                return [{'id': m['modelId'], 'name': m.get('name', m['modelId']), 'default': False} for m in result['availableModels']]
        finally:
            if process:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()


async def model_catalog(agent):
    if os.getenv('TRANSMUX_' + agent.upper() + '_MODELS') is not None:
        models = model_choices(agent)
        return {'models': models, 'source': 'configured', 'warning': '当前使用管理员指定的模型列表', 'fetched_at': time.time()}
    try:
        entries = await live_models(agent)
        entries = list({m['id']: m for m in entries if m['id']}.values())
        if not entries:
            raise ValueError('Agent 返回了空模型列表')
        return {'models': [m['id'] for m in entries], 'entries': entries, 'source': 'agent',
                'default_model': next((m['id'] for m in entries if m['default']), None), 'fetched_at': time.time()}
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        models = await asyncio.to_thread(model_choices, agent)
        return {'models': models, 'source': 'cache' if agent == 'codex' else 'cli_help', 'fetched_at': time.time(),
                'warning': ('实时查询失败，暂用本地缓存，可重试刷新' if agent == 'codex' else '实时查询失败，暂用 CLI 声明的模型列表，可重试刷新') if models else '未获取到模型列表，可重试或沿用 Agent 默认模型'}
