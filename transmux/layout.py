"""Versioned original-format exports. Rendering never rewrites the DOCX."""

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import time
from zipfile import ZipFile

from lxml import etree
from pypdf import PdfReader

from .store import atomic_write, uid
from .presets import PRESETS, apply_preset


ORIGINAL = {"id": "original", "version": 1, "name": "保留原格式"}


def converter_command():
    return shutil.which(os.getenv("TRANSMUX_LIBREOFFICE", "libreoffice")) or shutil.which("soffice")


def inspect_docx(path):
    """Read-only package checks; refuse active/linked content before rendering."""
    issues = []
    with ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
            raise ValueError("DOCX 解压后超过 100 MB")
        names = archive.namelist()
        if "word/document.xml" not in names:
            raise ValueError("无效 DOCX 文档")
        for name in names:
            if "vbaproject" in name.lower() or name.startswith("word/embeddings/"):
                issues.append("文档包含宏或嵌入对象，保留原 DOCX；请移除这些对象后生成 PDF。")
            if not name.endswith((".xml", ".rels")):
                continue
            content = archive.read(name)
            if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
                raise ValueError("文档包含不支持的 XML 实体定义")
            root = etree.fromstring(content, etree.XMLParser(resolve_entities=False, no_network=True))
            if name.endswith(".rels"):
                for relation in root:
                    if relation.get("TargetMode") == "External" and not relation.get("Type", "").endswith("/hyperlink"):
                        issues.append("文档包含外部链接资源，保留原 DOCX；请将资源嵌入文档后生成 PDF。")
            instructions = " ".join(root.xpath("//*[local-name()='instrText']/text()"))
            instructions += " " + " ".join(root.xpath("//*[local-name()='fldSimple']/@*[local-name()='instr']"))
            if re.search(r"\b(DDEAUTO|DDE|INCLUDETEXT|INCLUDEPICTURE|LINK|DATABASE|RD)\b", instructions, re.I):
                issues.append("文档包含外部数据字段，保留原 DOCX；请将字段转换为静态内容后生成 PDF。")
    return list(dict.fromkeys(issues))


async def render_pdf(docx, directory):
    executable = converter_command()
    if not executable:
        raise ValueError("服务器未配置 LibreOffice，DOCX 已保留；配置后可重新生成 PDF。")
    profile = directory / "profile"
    profile.mkdir()
    process = None
    try:
        with (directory / "renderer.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                executable, "-env:UserInstallation=" + profile.resolve().as_uri(),
                "--headless", "--norestore", "--nodefault", "--nofirststartwizard",
                "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(directory.resolve()), str(docx.resolve()),
                stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=120)
            except TimeoutError as exc:
                raise ValueError("PDF 转换超过 120 秒，DOCX 已保留；请稍后重试。") from exc
        pdf = directory / (docx.stem + ".pdf")
        if process.returncode != 0 or not pdf.is_file():
            raise ValueError("PDF 转换失败，DOCX 已保留；请检查服务器转换环境后重试。")
        try:
            with pdf.open("rb") as stream:
                pages = len(PdfReader(stream).pages)
        except Exception as exc:
            raise ValueError("转换结果不是有效 PDF，DOCX 已保留。") from exc
        if not pages:
            raise ValueError("PDF 没有可用页面，DOCX 已保留。")
        return {"engine": "LibreOffice", "pages": pages}
    finally:
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        shutil.rmtree(profile, ignore_errors=True)


def eligible(file):
    return file["kind"] in ("source", "manuscript", "output", "edited") and Path(file["name"]).suffix.lower() == ".docx"


def export_record(store, pid, eid):
    store.project(pid)
    rows = store.rows("SELECT * FROM layout_exports WHERE project=? AND id=?", (pid, eid))
    if not rows:
        raise ValueError("导出版本不存在")
    row = rows[0]
    row["manifest"] = json.loads(row["manifest"])
    return row


def export_path(store, pid, eid, format):
    record = export_record(store, pid, eid)
    if format not in ("docx", "pdf", "json") or format == "pdf" and not record["manifest"]["pdf"]:
        raise ValueError("此版本没有可用的 PDF")
    root = store.root / "exports" / pid / eid
    path = root / ("manifest.json" if format == "json" else "document." + format)
    if path.is_symlink() or path.resolve().parent != root or not path.is_file():
        raise ValueError("导出文件不存在")
    return path, record


async def export_original(worker, job, payload):
    store, pid = worker.store, job["project"]
    file = store.file(pid, payload["file_id"])
    template = payload.get('template', 'original')
    if not eligible(file) or template != 'original' and template not in PRESETS:
        raise ValueError("请选择可用的 DOCX 排版模板")
    worker.phase(job, "export_snapshot", "保存导出版本", "保留 DOCX 的全部内容和原有格式")
    eid = uid()
    root = store.root / "exports" / pid
    root.mkdir(parents=True, exist_ok=True)
    staging = root / ("pending-" + eid)
    staging.mkdir()
    published = root / eid
    try:
        docx = staging / "document.docx"
        shutil.copyfile(store.download_path(pid, file), docx)
        digest = hashlib.sha256(docx.read_bytes()).hexdigest()
        issues = await worker.blocking(inspect_docx, docx)
        details = None
        if template in PRESETS:
            if issues:
                raise ValueError('此文档含不支持的嵌入或外部资源，请使用保留原格式导出。')
            worker.phase(job, 'export_layout', '应用 ' + PRESETS[template]['name'], '识别文档结构，应用标题、正文、题注与双栏格式')
            original = staging / 'input.docx'
            docx.rename(original)
            roles, expected = payload.get('roles'), payload.get('source_sha256')
            semantic = None
            formatted_source = original
            if hasattr(worker, 'semantic_layout'):
                formatted_source = staging / 'semantic.docx'
                semantic = await worker.semantic_layout(job, original, formatted_source, template)
                roles = semantic['roles_after_render']
                expected = hashlib.sha256(formatted_source.read_bytes()).hexdigest()
            details = await worker.blocking(apply_preset, formatted_source, docx, template, roles, expected)
            if semantic is not None:
                from .semantic_layout import report
                details['semantic_layout'] = semantic
                details['findings'] = [f for f in details['findings'] if f.get('code') != 'references_pending']
                details['findings'] += [{'code': 'semantic_review', 'message': item} for item in semantic['issues']]
                if semantic['uncited']:
                    details['findings'].append({'code': 'uncited_references', 'message': f"{len(semantic['uncited'])} 条参考文献未找到正文锚点，已保留在末尾。"})
                if semantic['unresolved']:
                    details['findings'].append({'code': 'unresolved_anchors', 'message': f"{len(semantic['unresolved'])} 条引文锚点需人工复核，详见处理报告。"})
                details['checks'][0]['message'] = '非引文正文和原对象保留；原文锚点按已校验位置改写，所有参考文献均保留。'
                atomic_write(staging / 'layout-report.md', report(semantic))
        if details and "semantic_layout" not in details and hasattr(worker, "format_citations"):
            citation_result = await worker.format_citations(job, docx, payload, details)
            details["citation_formatting"] = citation_result
            details["checks"][0]["message"] = '正文保留；仅允许锚定的引文标记与已有参考文献信息的呈现调整。'
            if citation_result['applied']:
                details['findings'].append({'id': 'citation_changes', 'message': f"已应用 {citation_result['applied']} 处引文呈现调整，请复核引用关系。"})
            details["findings"] = [f for f in details["findings"] if f.get("id") != "references_pending"]
            details["findings"] += [{"id": "citation_review", "message": issue} for issue in citation_result["issues"]]
        output_digest = hashlib.sha256(docx.read_bytes()).hexdigest()
        renderer = None
        if not issues:
            worker.phase(job, "export_pdf", "生成 PDF 预览", "正在转换文档，完成后可预览和下载")
            try:
                renderer = await render_pdf(docx, staging)
            except (ValueError, OSError) as exc:
                issues.append(str(exc))
        if hashlib.sha256(docx.read_bytes()).hexdigest() != output_digest:
            raise ValueError("转换期间 DOCX 意外变化，未发布此导出版本")
        if renderer is None:
            (staging / "document.pdf").unlink(missing_ok=True)
        manifest = {
            "schema_version": 2, "template": ORIGINAL if template == 'original' else PRESETS[template], "source_file": file["id"], "source_name": file["name"],
            "source_sha256": digest, "docx_sha256": output_digest, "pdf": renderer is not None,
            "renderer": renderer, "issues": issues, "checks": [{"id": "unchanged_docx", "passed": True,
            "message": "导出 DOCX 与输入文件字节完全一致；未修改内容、排版或引文。"}],
            "notice": "PDF 为转换预览，字体替代与分页可能和 Word 不同，请检查图片、表格和公式。未执行期刊模板或文献核验。",
        }
        if details:
            manifest.update(details)
            manifest['issues'] = issues + [finding['message'] for finding in details['findings']]
            manifest['notice'] = '基于官方 Word 模板生成的期刊排版草稿，未核验文献信息，不代表全部期刊要求已满足。请审阅结构、字体替代、复杂对象与分页。'
        manifest['status'] = 'docx_only' if not renderer else 'draft' if details and details['findings'] else 'ready'
        if renderer:
            manifest["pdf_sha256"] = hashlib.sha256((staging / "document.pdf").read_bytes()).hexdigest()
        encoded = json.dumps(manifest, ensure_ascii=False, indent=2)
        atomic_write(staging / "manifest.json", encoded)
        worker.phase(job, "export_publish", "保存导出结果", "正在保存文件、校验记录和版本历史")
        staging.rename(published)
        try:
            store.execute("INSERT INTO layout_exports VALUES (?,?,?,?,?,?)",
                          (eid, pid, file["id"], job["id"], encoded, time.time()))
        except BaseException:
            shutil.rmtree(published)
            raise
        message = '排版草稿已生成，请在排版与导出中检查待处理项。' if manifest['status'] == 'draft' else 'DOCX 与 PDF 已生成，可在排版与导出中查看。'
        return {"export_id": eid, "name": file["name"], "status": manifest['status'],
                "message": message if renderer else "DOCX 已保存，PDF 未生成：" + "；".join(issues)}
    finally:
        if staging.exists():
            shutil.rmtree(staging)
