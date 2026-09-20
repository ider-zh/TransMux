import json

import pytest
from fastapi.testclient import TestClient

from transmux.app import create_app
from transmux.jobs import Worker
from transmux.rag import Rag
from transmux.store import Store
from transmux import terminology as terms
from test_workflows import FakeRunner, TinyEmbeddings, add_source, job


def document(store, pid, kind):
    return json.loads(store.snapshot_config(pid, kind)["content"])


def test_manual_changes_deletions_and_restart_survive_automatic_merges(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project("test", "codex")["id"]
    auto = dict(term="doctor", meaning="medical professional", usage="original", source="corpus.txt")
    assert store.merge_terminology(pid, "terms", [auto], "extraction", "job1") == 1
    current = store.snapshot_config(pid, "terms")
    edited = json.loads(current["content"])
    edited["rows"][0]["usage"] = "User requirement"
    store.write_config(pid, "terms", terms.encode(edited), current["revision"])
    store.merge_terminology(pid, "terms", [{**auto, "usage":"Overwrite attempt"}], "extraction", "job2")
    assert document(store, pid, "terms")["rows"][0]["usage"] == "User requirement"
    assert document(store, pid, "terms")["rows"][0]["origin"] == "manual"
    current = store.snapshot_config(pid, "terms")
    edited["rows"] = []
    store.write_config(pid, "terms", terms.encode(edited), current["revision"])
    store.db.close()
    store = Store(tmp_path)
    assert store.merge_terminology(pid, "terms", [auto], "extraction", "job3") == 0
    assert document(store, pid, "terms")["rows"] == []
    store.db.close()


def test_legacy_migration_is_lossless_and_idempotent(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project("test", "codex")["id"]
    work = store.workspace(pid)
    legacy = "# 自定义说明\n不要翻译品牌。\n| 原文 | 译文 | 说明 |\n| --- | --- | --- |\n| 医生 | doctor | 医学 |\n"
    (work / "glossary.md").write_text(legacy)
    (work / "mappings.json").unlink()
    store.ensure_terminology(pid)
    initial = document(store, pid, "mappings")
    assert initial["legacy"] == legacy
    assert initial["rows"][0]["original"] == "医生"
    assert initial["rows"][0]["origin"] == "manual"
    assert (work / "glossary.md").read_text() == legacy
    store.ensure_terminology(pid)
    assert document(store, pid, "mappings") == initial
    assert document(store, pid, "terms")["rows"] == []
    store.db.close()


class PairRunner(FakeRunner):
    async def run(self, pid, jid, prompt, schema=None):
        result = await super().run(pid, jid, prompt, schema)
        if schema and "translations" in schema["properties"]:
            result["mappings"] = [dict(original="doctor", translation="医生", context="医学", paragraph=1, need='preferred_variant', reason='项目需统一首选译法，避免同一职务或机构名称混用')]
        return result


@pytest.mark.parametrize("passed", [True, False])
async def test_only_reviewed_pairs_are_accumulated_without_extra_calls(tmp_path, passed):
    store = Store(tmp_path)
    pid = store.create_project("test", "codex", target_language="zh-CN")["id"]
    runner = PairRunner(passed)
    worker = Worker(store, runner, Rag(TinyEmbeddings()))
    task = job(store, pid, add_source(store, pid), rounds=1)
    await worker.execute(task)
    assert len(runner.calls) == 2
    assert '"mappings": [{"original": "doctor"' in runner.calls[1][2]
    rows = document(store, pid, "mappings")["rows"]
    assert bool(rows) is passed
    if passed:
        assert rows[0]["origin"] == "translation"
        assert "段落 1" in rows[0]["source"]
        result = json.loads(store.rows("SELECT result FROM jobs WHERE id=?", (task["id"],))[0]["result"])
        assert result["new_mappings"] == 1
    store.db.close()


async def test_concurrent_manual_term_is_preserved_during_extraction(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project("test", "codex")["id"]
    add_source(store, pid, "corpus")

    class EditingRunner(FakeRunner):
        async def run(self, *args):
            current = store.snapshot_config(pid, "terms")
            data = json.loads(current["content"])
            data["rows"] = [dict(term="doctor", meaning="User meaning", usage="Fixed rule", source="用户", origin="manual")]
            store.write_config(pid, "terms", terms.encode(data), current["revision"])
            return await super().run(*args)

    await Worker(store, EditingRunner(), Rag(TinyEmbeddings())).execute(store.enqueue(pid, "style", {}))
    assert store.rows("SELECT state FROM jobs")[0]["state"] == "succeeded"
    assert document(store, pid, "terms")["rows"][0]["meaning"] == "User meaning"
    assert document(store, pid, "mappings")["rows"] == []
    store.db.close()


def test_terms_history_restore_is_separate_and_uses_revision(tmp_path, monkeypatch):
    monkeypatch.setattr("transmux.app.availability", lambda: [{"id":"codex", "available":True}])
    with TestClient(create_app(tmp_path)) as client:
        pid = client.post("/api/projects", json=dict(name="test", agent="codex")).json()["id"]
        base = f"/api/projects/{pid}/config/"
        before = client.get(base + "terms").json()
        old = client.get(base + "terms/history").json()[0]
        mappings = client.get(base + "mappings").json()
        data = json.loads(before["content"])
        data["rows"] = [dict(term="doctor", meaning="", usage="", source="", origin="extraction")]
        saved = client.put(base + "terms", json=dict(content=terms.encode(data), revision=before["revision"])).json()
        assert json.loads(saved["content"])["rows"][0]["origin"] == "manual"
        restore = base + f"terms/history/{old['id']}/restore"
        assert client.post(restore, json=dict(revision=before["revision"])).status_code == 409
        assert client.post(restore, json=dict(revision=saved["revision"])).status_code == 200
        assert client.get(base + "mappings").json() == mappings
        assert client.put(base + "terms", json=dict(content="bad JSON", revision=client.get(base + "terms").json()["revision"])).status_code == 400


def test_fabricated_evidence_is_rejected():
    with pytest.raises(ValueError, match="原文"):
        Worker.evidenced_rows([dict(term="invented", meaning="", usage="", paragraph=1)], ["doctor"], "terms")
    with pytest.raises(ValueError, match="译文"):
        Worker.evidenced_rows([dict(original="doctor", translation="律师", context="", paragraph=1, need='preferred_variant', reason='项目需统一首选译法，避免同一职务或机构名称混用')], ["doctor"], "mappings", ["医生"])


def test_extraction_repairs_only_unambiguous_evidence_and_normalizes_typography():
    samples = ["Introduction", "Plato’s Problem concerns knowledge. Computational\nlinguistics.",
               "The transformational‑generative grammar theory.", "A shared concept.", "Another shared concept."]
    rows = [dict(term=term, meaning="含义", usage="规则", paragraph=paragraph) for term, paragraph in (
        ("Plato's Problem", 1), ("computational linguistics", 2),
        ("transformational-generative grammar", 99), ("invented concept", 1),
        ("shared concept", 1), ("shared concept", 4), ("", 1))]
    rows.append({"term":"bad schema"})
    accepted, report = Worker.extracted_terms(rows, samples)
    assert [(row["term"], row["paragraph"]) for row in accepted] == [
        ("Plato's Problem", 2), ("computational linguistics", 2),
        ("transformational-generative grammar", 3), ("shared concept", 4)]
    assert sum(row["status"] == "corrected" for row in report) == 2
    assert sum(row["status"] == "skipped" for row in report) == 4
    with pytest.raises(ValueError, match="数组"):
        Worker.extracted_terms("invalid", samples)


async def test_bad_extracted_term_does_not_discard_style_or_valid_terms(tmp_path):
    store = Store(tmp_path)
    pid = store.create_project("test", "codex")["id"]
    add_source(store, pid, "corpus")

    class WrongCitationRunner(FakeRunner):
        async def run(self, *args):
            response = await super().run(*args)
            if "terms" in response:
                response["terms"][0]["paragraph"] = 2
                response["terms"].append(dict(term="fabricated term", meaning="", usage="", paragraph=1))
            return response

    runner = WrongCitationRunner()
    task = store.enqueue(pid, "style", {})
    await Worker(store, runner, Rag(TinyEmbeddings())).execute(task)
    result = store.rows("SELECT state,result FROM jobs WHERE id=?", (task["id"],))[0]
    assert result["state"] == "succeeded"
    assert "跳过 1 条" in result["result"]
    assert len(runner.calls) == 2
    rows = document(store, pid, "terms")["rows"]
    assert len(rows) == 1 and rows[0]["term"] == "doctor" and "段落 1" in rows[0]["source"]
    work = store.workspace(pid)
    assert "Translation Style" in (work / "style.md").read_text()
    assert (work / "rag/index.json").exists()
    report = json.loads((work / "runs" / task["id"] / "style-batch-1-validation.json").read_text())
    assert [row["status"] for row in report] == ["corrected", "skipped"]
    store.db.close()
