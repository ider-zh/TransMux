import asyncio
import contextlib
import json
import os
import shutil
import signal
import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values


def model_choices(agent):
    configured = os.getenv("TRANSMUX_" + agent.upper() + "_MODELS")
    if configured is not None:
        return list(dict.fromkeys(m.strip() for m in configured.split(",") if m.strip()))
    try:
        if agent == "codex":
            cache = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json"
            models = json.loads(cache.read_text())["models"]
            return [m["slug"] for m in models if m.get("visibility", "list") == "list" and m.get("slug")]
        result = subprocess.run([os.getenv("TRANSMUX_CODEBUDDY_BIN", "codebuddy"), "--help"],
                                capture_output=True, text=True, timeout=10, env=agent_environment(agent))
        match = re.search(r"Currently supported: \(([^)]+)\)", result.stdout)
        return [m.strip() for m in match[1].split(",")] if match else []
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return []


def availability():
    return [{"id": name, "available": bool(shutil.which(os.getenv("TRANSMUX_" + name.upper() + "_BIN", name)))}
            for name in ("codex", "codebuddy")]


def agent_environment(agent):
    """Load operator-owned settings, never a document workspace's .env or shell code."""
    config_path = Path(os.getenv('TRANSMUX_ENV_FILE', str(Path(__file__).resolve().parent.parent / '.env')))
    settings = dotenv_values(config_path, interpolate=False)
    child = os.environ.copy()
    for name in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        key = f'TRANSMUX_{agent.upper()}_{name.upper()}'
        value = os.environ.get(key, settings.get(key))
        if value is not None:
            child[name] = value
    return child


def command(agent, session, schema_path=None, model=None):
    binary = os.getenv("TRANSMUX_" + agent.upper() + "_BIN", agent)
    if agent == "codex":
        args = [binary, "-a", "never", "-s", "workspace-write", "exec"]
        if session:
            args += ["resume", session]
        args += ["--json", "--skip-git-repo-check"]
        if model:
            args += ["--model", model]
        if schema_path:
            args += ["--output-schema", str(schema_path)]
        return args + ["-"]
    if agent == "codebuddy":
        args = [binary, "-p", "--verbose", "--output-format", "stream-json", "--permission-mode", "acceptEdits"]
        if os.getenv("TRANSMUX_CODEBUDDY_SKIP_PERMISSIONS", "1") == "1":
            args += ["-y"]
        if session:
            args += ["--resume", session]
        if model:
            args += ["--model", model]
        if schema_path:
            args += ["--json-schema", schema_path.read_text()]
        return args
    raise ValueError("不支持的 Agent")


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    wrapper = re.fullmatch(r"<StructuredOutput>\s*(.*?)\s*</StructuredOutput>", text, re.S)
    if wrapper:
        text = wrapper[1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Agent 必须返回 JSON 对象")
    return value


class ContextLimitError(RuntimeError):
    """An explicit provider context limit, not a generic CLI/process failure."""


def error_text(event):
    errors = event.get('errors')
    if isinstance(errors, list) and errors:
        return '; '.join(str(error) for error in errors)
    details = event.get('errors_info')
    if isinstance(details, list) and details:
        return '; '.join(str(item.get('details', item)) if isinstance(item, dict) else str(item) for item in details)
    return str(event.get('error') or event.get('result') or event)


def context_limit(message):
    return bool(re.search(r'prompt is too long|context[_ ]length[_ ]exceeded|maximum context length|context window exceeded', message, re.I))


class AgentRunner:
    def __init__(self, store):
        self.store = store

    async def run(self, pid, jid, prompt, schema=None):
        project = self.store.project(pid)
        # Recover an already-failed pre-upgrade session without sending it back to the CLI.
        if project['agent'] == 'codebuddy' and project['session']:
            old_failures = self.store.rows("SELECT result FROM jobs WHERE project=? AND state='failed' AND instr(result,?)>0 ORDER BY created DESC LIMIT 5",
                                           (pid, project['session']))
            if any(row['result'].startswith('codebuddy 调用失败') and context_limit(row['result']) for row in old_failures):
                self.store.execute('UPDATE projects SET session=NULL WHERE id=?', (pid,))
                self.store.event(pid, jid, 'progress', '检测到原 CodeBuddy 会话曾因上下文超限失败，已改用新会话；历史记录保留，历史对话不会自动带入。')
                project = self.store.project(pid)
        resumed = bool(project['session'])
        try:
            return await self._run_once(pid, jid, prompt, schema)
        except ContextLimitError as exc:
            # Files and historical CLI sessions are retained. Only the next-call pointer changes.
            previous = self.store.project(pid)['session']
            self.store.execute('UPDATE projects SET session=NULL WHERE id=?', (pid,))
            self.store.event(pid, jid, 'progress',
                             f'CodeBuddy 上下文超限，已停止复用会话 {previous or "（未返回会话标识）"}；项目文件与任务记录保留，新会话不自动带入历史对话。')
            if not schema or not resumed:
                raise RuntimeError('CodeBuddy 上下文超过模型上限，已重置会话。'
                                   '当前调用未自动重放；请检查已生成文件后重新提交任务。'
                                   '新会话不会自动包含历史对话，请补充必要背景。') from exc
            self.store.event(pid, jid, 'progress',
                             '正在以新会话重试当前结构化步骤（最多一次），沿用本次任务的模型、配置快照和输入。')
            try:
                return await self._run_once(pid, jid, prompt, schema)
            except ContextLimitError as retry_error:
                self.store.execute('UPDATE projects SET session=NULL WHERE id=?', (pid,))
                raise RuntimeError('CodeBuddy 新会话仍超过上下文上限，已停止重试。'
                                   '请缩小本次输入或在项目右上角切换适用模型后重试；项目文件与记录保留。') from retry_error

    async def run_isolated(self, pid, jid, prompt, schema=None):
        """Stateless extraction steps must not accumulate in the chat session."""
        return await self._run_once(pid, jid, prompt, schema, isolated=True)

    async def _run_once(self, pid, jid, prompt, schema=None, isolated=False):
        project = self.store.project(pid)
        work = self.store.workspace(pid)
        schema_path = None
        if schema:
            schema_path = work / "runs" / jid / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
        job = self.store.rows("SELECT payload FROM jobs WHERE id=? AND project=?", (jid, pid))
        payload = json.loads(job[0]["payload"]) if job else {}
        model = payload.get("_model", project.get("model"))
        args = command(project["agent"], None if isolated else project["session"], schema_path, model)
        if payload.get('_workspace_v2'):
            if project['agent'] == 'codebuddy':
                args = [arg for arg in args if arg != '-y']
                args[args.index('--permission-mode') + 1] = 'dontAsk'
                args += ['--tools', 'StructuredOutput,WebSearch,WebFetch' if payload.get('external_research') else 'StructuredOutput',
                         '--allowedTools', 'StructuredOutput,WebSearch,WebFetch' if payload.get('external_research') else 'StructuredOutput',
                         '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--max-turns', '20',
                         '--include-partial-messages']
            else:
                args[args.index('-s') + 1] = 'read-only'
                # Workspace tasks need built-in search, not the host's unrelated MCP integrations.
                import tomllib
                config = Path(os.getenv('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
                try:
                    servers = tomllib.loads(config.read_text()).get('mcp_servers', {})
                except (OSError, ValueError):
                    servers = {}
                for name in servers:
                    args[1:1] = ['-c', 'mcp_servers.' + name + '.enabled=false']
        if payload.get("external_research") and project["agent"] == "codex":
            args.insert(1, "--search")
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=work, env=agent_environment(project["agent"]),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=4 * 1024 * 1024)
        result = ""
        failure = None

        async def stdout():
            nonlocal result, failure
            async for line in proc.stdout:
                raw = line.decode("utf-8", errors="replace").strip()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    self.store.event(pid, jid, "progress", raw[:8000])
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type", "event")
                session = event.get("thread_id") if kind == "thread.started" else event.get("session_id")
                if session and not isolated:
                    self.store.execute("UPDATE projects SET session=? WHERE id=?", (session, pid))
                if kind in ("turn.failed", "error") or event.get("is_error"):
                    failure = error_text(event)
                item = event.get("item", {})
                if kind == "item.completed" and item.get("type") == "agent_message":
                    result = item.get("text", "")
                if kind == "assistant":
                    content = event.get("message", {}).get("content", [])
                    text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
                    if project['agent'] == 'codebuddy' and re.fullmatch(
                            r'400 prompt is too long: \d+ tokens > \d+ maximum(?: \([^\n]+\))?', text.strip()):
                        self.store.event(pid, jid, 'agent', raw[:16000])
                        raise ContextLimitError(text)
                    if text:
                        result = text
                if kind == "result":
                    structured = event.get("structured_output")
                    result = json.dumps(structured, ensure_ascii=False) if structured is not None else event.get("result", result)
                self.store.event(pid, jid, "agent", raw[:16000])

        async def stderr():
            async for line in proc.stderr:
                self.store.event(pid, jid, "progress", line.decode("utf-8", errors="replace").strip()[:8000])

        async def exchange():
            out = asyncio.create_task(stdout())
            err = asyncio.create_task(stderr())
            try:
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
                await asyncio.gather(out, err)
                return await proc.wait()
            finally:
                for task in (out, err):
                    task.cancel()
                await asyncio.gather(out, err, return_exceptions=True)

        try:
            code = await asyncio.wait_for(exchange(), timeout=int(os.getenv("TRANSMUX_AGENT_TIMEOUT", "1800")))
        finally:
            # Also terminate any descendant retaining pipe handles on cancellation/timeout.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        if code != 0 or failure:
            if project['agent'] == 'codebuddy' and failure and context_limit(failure):
                raise ContextLimitError(failure)
            raise RuntimeError(f"{project['agent']} 调用失败 (exit={code}): {failure or '请查看进度日志'}")
        if not result:
            raise RuntimeError("Agent 未返回最终结果")
        parsed = parse_json(result) if schema else result
        if schema and payload.get("_workspace_v2"):
            from jsonschema import validate
            validate(parsed, schema)
        return parsed
