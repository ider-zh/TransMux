import asyncio
import contextlib
import fcntl
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agents import availability, model_choices
from . import terminology, term_import, term_review
from .documents import extract, legacy_alignment
from .jobs import Worker
from .store import ConfigConflict, Store, atomic_write, uid
from .layout import ORIGINAL, converter_command, eligible, export_path, inspect_docx
from .presets import PRESETS, ROLES, structure


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ModelInput(Input):
    model: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")

    @field_validator("model", mode="before")
    @classmethod
    def empty_model(cls, value):
        return value.strip() or None if isinstance(value, str) else value


class ProjectInput(ModelInput):
    name: str = Field(min_length=1, max_length=100)
    agent: Literal["codex", "codebuddy"]
    target_language: Literal["en", "zh-CN"] = "en"


class RagPreference(Input):
    use_rag: bool


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=5000000)
    revision: str


class RestoreInput(Input):
    revision: str


class DeleteProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class JobInput(Input):
    kind: Literal["style", "rag", "recall", "translate", "chat", "revise", "layout", "terminology_review"]
    template: Literal["original", "ieee-access", "jcst"] = "original"
    source_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    roles: dict[str, str] = Field(default_factory=dict, max_length=3000)

    @field_validator('roles')
    @classmethod
    def valid_roles(cls, value):
        if any(not key.startswith('b') or not key[1:].isdigit() or len(key) > 8 or role not in ROLES for key, role in value.items()):
            raise ValueError('无效文档结构修正')
        return value
    file_id: str | None = None
    paragraph: int | None = Field(default=None, ge=1)
    use_rag: bool | None = None
    max_review_rounds: int = Field(default=3, ge=1, le=10)
    message: str = Field(default="", max_length=20000)
    top_k: int = Field(default=3, ge=1, le=10)


class TermResolve(Input):
    revision: str
    row_index: int = Field(ge=0)
    action: Literal['activate', 'deactivate']


class TermPreview(Input):
    rows: list[dict[str, str]] = Field(max_length=5000)


class TermImport(Input):
    revision: str
    operations: list[dict] = Field(max_length=5000)


class ReviewDecision(Input):
    index: int = Field(ge=0)
    edits: dict[str, str] = Field(default_factory=dict)


class ReviewApply(Input):
    decisions: list[ReviewDecision] = Field(max_length=15000)


def create_app(root=None, worker_factory=Worker):
    root = Path(root or os.getenv("TRANSMUX_DATA", "data"))
    uploads = {}

    @asynccontextmanager
    async def lifespan(app):
        root.mkdir(parents=True, exist_ok=True)
        lock = (root / "service.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("该数据目录已由另一个服务使用；TransMux 必须以单 worker 启动")
        store = Store(root)
        worker = worker_factory(store)
        app.state.store, app.state.worker = store, worker
        task = asyncio.create_task(worker.loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            store.db.close()
            lock.close()

    app = FastAPI(title="TransMux", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.getenv(
        "TRANSMUX_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(","))

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "拒绝跨站写入请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(ConfigConflict)
    async def config_conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def store():
        return app.state.store

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "workflow_version": 15, "agents": availability(), "embedding": app.state.worker.rag.embeddings.identity}

    @app.get("/api/layout/capabilities")
    async def layout_capabilities():
        return {"templates": [ORIGINAL, *PRESETS.values()], "pdf_available": bool(converter_command())}

    @app.get('/api/projects/{pid}/files/{fid}/structure')
    async def layout_structure(pid: str, fid: str):
        file = store().file(pid, fid)
        if not eligible(file):
            raise HTTPException(400, '请选择 DOCX 文稿或译文')
        source = store().download_path(pid, file)
        await Worker.blocking(inspect_docx, source)
        result = await Worker.blocking(structure, source)
        store().file(pid, fid)
        return result

    @app.get("/api/projects")
    async def projects():
        return store().rows("SELECT * FROM projects ORDER BY created")

    @app.get("/api/agents/{agent}/models")
    async def models(agent: Literal["codex", "codebuddy"]):
        return {"models": await asyncio.to_thread(model_choices, agent)}

    @app.post("/api/projects", status_code=201)
    async def create_project(body: ProjectInput):
        if not next(a["available"] for a in availability() if a["id"] == body.agent):
            raise HTTPException(409, f"未找到 {body.agent} CLI，请先安装并登录")
        return store().create_project(body.name, body.agent, body.model, body.target_language)

    @app.patch("/api/projects/{pid}/rag-preference")
    async def rag_preference(pid: str, body: RagPreference):
        store().project(pid)
        store().execute("UPDATE projects SET use_rag=? WHERE id=?", (int(body.use_rag), pid))
        return store().project(pid)

    @app.get("/api/projects/{pid}/rag-status")
    async def rag_status(pid: str):
        project = store().project(pid)
        worker = app.state.worker
        work = store().workspace(pid)
        style_status = {"style_pending": store().corpus_style_pending(pid)}
        try:
            data = await Worker.blocking(worker.rag.load, work, worker.corpus(pid), project["target_language"])
            return {"state": "ready", "chunks": len(data["chunks"]), **data["counts"], **style_status}
        except (ValueError, OSError, KeyError):
            status_path = work / "rag" / "status.json"
            if status_path.exists():
                try:
                    status = json.loads(status_path.read_text())
                    if status.get("state") == "failed":
                        return {"state": "failed", "message": "参考索引更新失败，请重试", **style_status}
                except (ValueError, OSError):
                    pass
            return {"state": "pending", "message": "更新风格或翻译时自动同步参考索引", **style_status}

    @app.patch("/api/projects/{pid}")
    async def update_model(pid: str, body: ModelInput):
        store().project(pid)
        if "model" not in body.model_fields_set:
            raise HTTPException(400, "请指定模型")
        store().execute("UPDATE projects SET model=? WHERE id=?", (body.model, pid))
        store().event(pid, None, "progress", "项目模型已更新：" + (body.model or "沿用 Agent 设置") + "；运行中任务保持原模型")
        return store().project(pid)

    @app.get("/api/projects/{pid}")
    async def project(pid: str):
        return store().project(pid)

    @app.get("/api/projects/{pid}/config/{name}")
    async def get_config(pid: str, name: Literal["style", "requirements", "glossary", "terms", "mappings", "people"]):
        return store().snapshot_config(pid, name)

    @app.put("/api/projects/{pid}/config/{name}")
    async def save_config(pid: str, name: Literal["style", "requirements", "glossary", "terms", "mappings", "people"], body: ConfigInput):
        return store().write_config(pid, name, body.content, body.revision)

    @app.get("/api/projects/{pid}/config/{name}/history")
    async def config_history(pid: str, name: Literal["style", "requirements", "glossary", "terms", "mappings", "people"], before: int = 0):
        store().snapshot_config(pid, name)
        return store().rows("SELECT * FROM config_history WHERE project=? AND name=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 100",
                            (pid, name, before, before))

    @app.post("/api/projects/{pid}/config/{name}/history/{hid}/restore")
    async def restore_config(pid: str, name: Literal["style", "requirements", "glossary", "terms", "mappings", "people"], hid: int, body: RestoreInput):
        rows = store().rows("SELECT content FROM config_history WHERE project=? AND name=? AND id=?", (pid, name, hid))
        if not rows:
            raise HTTPException(404, "快照不存在")
        return store().write_config(pid, name, rows[0]["content"], body.revision, "restore")

    @app.post('/api/projects/{pid}/terminology/{kind}/resolve')
    async def resolve_term(pid: str, kind: Literal['terms', 'mappings', 'people'], body: TermResolve):
        current = store().snapshot_config(pid, kind)
        document = terminology.decode(kind, current['content'])
        if body.row_index >= len(document['rows']):
            raise ValueError('条目不存在，请重新载入')
        identity = terminology.key(kind, document['rows'][body.row_index])
        changed = terminology.resolve(kind, document, identity, body.action)
        return store().write_config(pid, kind, terminology.encode(changed), body.revision)

    @app.post('/api/projects/{pid}/terminology/parse')
    async def parse_terms(pid: str, file: UploadFile | None = File(None), text: str = Form('')):
        store().project(pid)
        data = await file.read(2 * 1024 * 1024 + 1) if file else text.encode('utf-8')
        try:
            return await asyncio.to_thread(term_import.parse_table, data, file.filename or 'table.csv' if file else 'paste.tsv')
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/projects/{pid}/terminology/{kind}/preview')
    async def preview_terms(pid: str, kind: Literal['terms', 'mappings', 'people'], body: TermPreview):
        current = store().snapshot_config(pid, kind)
        return {'revision': current['revision'], 'items': term_import.preview(kind, terminology.decode(kind, current['content']), body.rows)}

    @app.post('/api/projects/{pid}/terminology/{kind}/import')
    async def import_terms(pid: str, kind: Literal['terms', 'mappings', 'people'], body: TermImport):
        current = store().snapshot_config(pid, kind)
        proposed = term_import.apply_import(kind, terminology.decode(kind, current['content']), body.operations)
        return store().write_config(pid, kind, terminology.encode(proposed), body.revision)

    def review_proposal(pid, jid):
        rows = store().rows("SELECT state FROM jobs WHERE project=? AND id=? AND kind='terminology_review'", (pid, jid))
        if not rows or rows[0]['state'] != 'succeeded':
            raise ValueError('筛选预览不存在或尚未完成')
        path = store().safe_path(pid, f'runs/{jid}/terminology-proposal.json')
        return json.loads(path.read_text())

    @app.get('/api/projects/{pid}/terminology-review/{jid}')
    async def get_review_proposal(pid: str, jid: str):
        return review_proposal(pid, jid)

    @app.post('/api/projects/{pid}/terminology-review/{jid}/apply')
    async def apply_review_proposal(pid: str, jid: str, body: ReviewApply):
        proposal = review_proposal(pid, jid)
        documents = {kind: terminology.decode(kind, store().snapshot_config(pid, kind)['content']) for kind in terminology.KINDS}
        for kind in terminology.KINDS:
            if store().snapshot_config(pid, kind)['revision'] != proposal['revisions'][kind]:
                raise ConfigConflict('筛选后术语已变化，请重新筛选；未应用旧建议')
        updated = term_review.apply_proposal(documents, proposal, [d.model_dump() for d in body.decisions])
        return store().write_terminology_bundle(pid, updated, proposal['revisions'], jid)

    @app.get("/api/projects/{pid}/files")
    async def files(pid: str):
        store().project(pid)
        files = store().rows("""SELECT f.*,c.root_file,c.parent_file FROM files f LEFT JOIN comparisons c
            ON c.file=f.id AND c.project=f.project WHERE f.project=? ORDER BY f.created DESC""", (pid,))
        legacy = store().legacy_translation_jobs(pid)
        for file in files:
            file["can_compare"] = bool(file["root_file"] or file["id"] in legacy)
            file["can_layout"] = eligible(file)
        return files

    @app.get("/api/projects/{pid}/files/{fid}/exports")
    async def export_versions(pid: str, fid: str):
        store().file(pid, fid)
        rows = store().rows("SELECT * FROM layout_exports WHERE project=? AND source_file=? ORDER BY created DESC,rowid DESC", (pid, fid))
        for row in rows:
            row["manifest"] = json.loads(row["manifest"])
        return rows

    @app.get("/api/projects/{pid}/exports/{eid}/{format}")
    async def export_download(pid: str, eid: str, format: Literal["docx", "pdf", "json"], preview: bool = False):
        file_path, record = export_path(store(), pid, eid, format)
        name = Path(record["manifest"]["source_name"]).stem + "-export-" + eid[:8] + "." + format
        inline = preview and format == "pdf"
        return FileResponse(file_path, filename=name, content_disposition_type="inline" if inline else "attachment",
                            headers={"X-Frame-Options": "SAMEORIGIN"} if inline else None)

    async def ensure_comparison(pid, fid):
        file = store().file(pid, fid)
        if store().rows("SELECT file FROM comparisons WHERE project=? AND file=?", (pid, fid)):
            return store().comparison(pid, fid)
        legacy = store().legacy_translation_jobs(pid).get(fid)
        if file["kind"] != "output" or not legacy:
            raise ValueError("此文档缺少已审校的段落对应记录，暂不能对照")
        source = store().file(pid, legacy["source_file"])
        paragraphs = await Worker.blocking(legacy_alignment, store().workspace(pid), legacy["job"],
                                          store().safe_path(pid, source["path"]), store().download_path(pid, file))
        store().file(pid, fid)  # The project may have been deleted while reading documents.
        store().execute("INSERT OR IGNORE INTO comparisons VALUES (?,?,?,?,?,?,?,?)",
                        (fid, pid, source["id"], fid, None, legacy["job"], json.dumps(paragraphs, ensure_ascii=False), time.time()))
        return store().comparison(pid, fid)

    @app.get("/api/projects/{pid}/files/{fid}/comparison")
    async def comparison(pid: str, fid: str):
        data = await ensure_comparison(pid, fid)
        data["source_name"] = store().file(pid, data["source_file"])["name"]
        data["name"] = store().file(pid, fid)["name"]
        data["versions"] = store().rows("""SELECT f.id,f.name,c.parent_file,c.root_file,c.created FROM comparisons c
            JOIN files f ON c.file=f.id AND c.project=f.project WHERE c.project=? AND c.root_file=? ORDER BY c.created,c.rowid""",
                                       (pid, data["root_file"]))
        return data

    def require_no_upload(pid):
        if uploads.get(pid):
            raise HTTPException(409, "文件正在上传或解析，请完成后再删除")

    @app.delete("/api/projects/{pid}")
    async def delete_project(pid: str, body: DeleteProjectInput):
        require_no_upload(pid)
        store().delete_project(pid, body.name)
        return {"status": "deleted"}

    @app.delete("/api/projects/{pid}/files/{fid}")
    async def delete_corpus(pid: str, fid: str):
        require_no_upload(pid)
        store().delete_corpus(pid, fid)
        return {"status": "deleted"}

    @app.post("/api/projects/{pid}/files", status_code=201)
    async def upload(pid: str, kind: Literal["corpus", "source", "manuscript"], file: UploadFile = File(...)):
        work = store().workspace(pid)
        name = Path((file.filename or "document").replace("\\", "/")).name
        if len(name) > 180:
            raise HTTPException(400, "文件名过长")
        suffix = Path(name).suffix.lower()
        if kind == "manuscript" and suffix != ".docx":
            raise HTTPException(400, "独立排版当前仅支持 DOCX")
        if suffix not in (".docx", ".pdf", ".txt", ".md"):
            raise HTTPException(400, "支持 PDF、DOCX、TXT 和 Markdown")
        path = work / ("corpus" if kind == "corpus" else "sources") / (uid() + suffix)
        sidecar = Path(str(path) + ".json")
        uploads[pid] = uploads.get(pid, 0) + 1
        try:
            size = 0
            with path.open("wb") as target:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 25 * 1024 * 1024:
                        raise HTTPException(413, "文件超过 25 MB")
                    target.write(chunk)
            try:
                if kind == "manuscript":
                    await Worker.blocking(inspect_docx, path)
                    blocks = []
                else:
                    blocks = await Worker.blocking(extract, path)
            except Exception as exc:
                raise HTTPException(400, f"文档解析失败：{exc}") from exc
            atomic_write(sidecar, json.dumps(blocks, ensure_ascii=False))
            fid = store().add_file(pid, name, kind, path)
            return dict(store().file(pid, fid), paragraphs=len(blocks))
        except BaseException:
            path.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            raise
        finally:
            uploads[pid] -= 1
            if not uploads[pid]:
                del uploads[pid]
            await file.close()

    @app.get("/api/projects/{pid}/files/{fid}/download")
    async def download(pid: str, fid: str):
        file = store().file(pid, fid)
        return FileResponse(store().download_path(pid, file), filename=file["name"])

    @app.get("/api/projects/{pid}/artifacts")
    async def artifacts(pid: str):
        work = store().workspace(pid)
        return [{"path": str(p.relative_to(work)), "name": p.name}
                for p in sorted((work / "runs").glob("*/*"))
                if p.is_file() and not p.is_symlink() and p.suffix in (".json", ".md")]

    @app.get("/api/projects/{pid}/artifact/{relative:path}")
    async def artifact(pid: str, relative: str):
        if not relative.startswith("runs/") or Path(relative).suffix not in (".json", ".md"):
            raise HTTPException(400, "无效的任务文件")
        path = store().safe_path(pid, relative)
        if not path.is_relative_to(store().workspace(pid) / "runs"):
            raise HTTPException(400, "无效的任务文件")
        return FileResponse(path, filename=path.name)

    @app.get("/api/projects/{pid}/jobs")
    async def jobs(pid: str):
        store().project(pid)
        return store().rows("SELECT * FROM jobs WHERE project=? ORDER BY created DESC LIMIT 100", (pid,))

    @app.post("/api/projects/{pid}/jobs", status_code=202)
    async def enqueue(pid: str, body: JobInput):
        store().project(pid)
        if body.kind in ("chat", "recall") and not body.message:
            raise HTTPException(400, "请输入内容")
        if body.kind == "translate":
            if not body.file_id or store().file(pid, body.file_id)["kind"] != "source":
                raise HTTPException(400, "请选择待翻译文件")
        if body.kind == "layout":
            if not body.file_id or not eligible(store().file(pid, body.file_id)):
                raise HTTPException(400, "请选择需要导出的 DOCX 文稿或译文")
            if body.template != 'original' and not body.source_sha256:
                raise HTTPException(400, '请先读取并检查文档结构')
            if store().rows("SELECT id FROM jobs WHERE project=? AND kind='layout' AND state IN ('queued','running') AND json_extract(payload,'$.file_id')=?",
                            (pid, body.file_id)):
                raise HTTPException(409, "此文档已有导出任务，请等待完成或停止后重试")
        if body.kind == "revise":
            if not body.file_id or not body.paragraph or not body.message:
                raise HTTPException(400, "请选择译文段落并填写修改要求")
            await ensure_comparison(pid, body.file_id)
            comparison = store().require_revision_available(pid, body.file_id)
            if body.paragraph > len(comparison["paragraphs"]):
                raise HTTPException(400, "段落编号无效")
        job = store().enqueue(pid, body.kind, body.model_dump(exclude={"kind"}))
        app.state.worker.wake.set()
        return job

    @app.post("/api/projects/{pid}/jobs/{jid}/cancel")
    async def cancel(pid: str, jid: str):
        rows = store().rows("SELECT * FROM jobs WHERE project=? AND id=?", (pid, jid))
        if not rows:
            raise HTTPException(404, "任务不存在")
        job = rows[0]
        worker = app.state.worker
        if job["state"] == "queued":
            worker.finish(job, "cancelled", "已从队列移除")
        elif worker.cancel(pid, jid):
            pass
        else:
            raise HTTPException(409, "任务已结束")
        return {"status": "cancellation_requested"}

    @app.get("/api/projects/{pid}/events")
    async def events(pid: str, after: int = 0):
        store().project(pid)
        return store().rows("SELECT * FROM events WHERE project=? AND id>? ORDER BY id LIMIT 300", (pid, after))

    @app.get("/api/projects/{pid}/stream")
    async def stream(pid: str, request: Request, after: int = 0):
        store().project(pid)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "无效事件游标")

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                rows = store().rows("SELECT * FROM events WHERE project=? AND id>? ORDER BY id LIMIT 100", (pid, cursor))
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not rows:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="frontend")
    return app


app = create_app()


def main():
    import uvicorn
    uvicorn.run("transmux.app:app", host=os.getenv("TRANSMUX_HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), workers=1)
