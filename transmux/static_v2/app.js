const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  pid: null,
  projects: [],
  files: [],
  artifacts: [],
  jobs: [],
  events: [],
  selected: new Map(),
  kind: "chat",
  stream: null,
  preview: null,
  templates: [],
  busy: false,
  generation: 0,
};
const labels = {
  style: "提取翻译风格",
  translate: "翻译文档",
  layout: "文档排版",
  factcheck: "事实核查",
  factfix: "修订核查结果",
  chat: "对话",
};
const statuses = {
  queued: "排队等待",
  running: "Agent 正在工作",
  succeeded: "已完成",
  failed: "执行失败",
  needs_attention: "需处理",
  cancelled: "已停止",
  interrupted: "已中断",
};
const configs = {
  style: "翻译风格.md",
  requirements: "用户要求.md",
  terms: "术语规范.json",
  mappings: "翻译对照.json",
  people: "人名规范.json",
};
const terminal = new Set([
  "succeeded",
  "failed",
  "needs_attention",
  "cancelled",
  "interrupted",
]);
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("toast").hidden = true), 6500);
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body instanceof FormData
        ? {}
        : { "Content-Type": "application/json" }),
      ...options.headers,
    },
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务器返回了无法读取的响应");
  }
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  return data;
}
const base = () => `/api/projects/${state.pid}`;
function listen(id, event, fn) {
  $(id).addEventListener(event, async (e) => {
    try {
      await fn(e);
    } catch (error) {
      toast(error.message);
    }
  });
}
function md(text) {
  return esc(text)
    .split("\n")
    .map((line) => {
      let content = line
        .replace(
          /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
          '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
        )
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/`([^`]+)`/g, "<code>$1</code>");
      if (/^### /.test(line)) return `<h3>${content.slice(4)}</h3>`;
      if (/^## /.test(line)) return `<h2>${content.slice(3)}</h2>`;
      if (/^# /.test(line)) return `<h1>${content.slice(2)}</h1>`;
      if (line.startsWith("&gt; "))
        return `<blockquote>${content.slice(5)}</blockquote>`;
      return `<div>${content || "<br>"}</div>`;
    })
    .join("");
}
function project() {
  return state.projects.find((p) => p.id === state.pid);
}
async function loadProjects() {
  state.projects = await api("/api/projects");
  $("workspaces").innerHTML = state.projects
    .map(
      (p) =>
        `<button data-project="${p.id}" class="${p.id === state.pid ? "active" : ""}">◻ &nbsp;${esc(p.name)}</button>`,
    )
    .join("");
  $("workspaces")
    .querySelectorAll("[data-project]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          selectProject(el.dataset.project).catch((e) => toast(e.message))),
    );
}
async function selectProject(pid) {
  if (state.preview?.dirty && !confirm("放弃未保存的编辑？")) return;
  state.generation++;
  const generation = state.generation;
  state.stream?.close();
  state.pid = pid;
  state.selected.clear();
  state.events = [];
  state.jobs = [];
  state.files = [];
  state.preview = null;
  closePreview(true);
  setTask("chat");
  localStorage.setItem("transmux-v2-workspace", pid);
  await loadProjects();
  if (generation !== state.generation) return;
  const p = project();
  $("workspaceName").textContent = p.name;
  $("workspaceInfo").textContent =
    `${p.target_language === "en" ? "English" : "简体中文"} · 单一对话`;
  $("agentLabel").textContent = p.agent;
  const data = await api(`/api/agents/${p.agent}/models`);
  if (generation !== state.generation) return;
  const models = [
    ...new Set([...(data.models || []), ...(p.model ? [p.model] : [])]),
  ];
  $("model").innerHTML =
    '<option value="">Agent 默认模型</option>' +
    models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
  $("model").value = p.model || "";
  await refresh();
  if (generation !== state.generation) return;
  state.stream = new EventSource(base() + "/stream");
  state.stream.onmessage = (e) => {
    if (generation !== state.generation) return;
    const event = JSON.parse(e.data);
    if (!state.events.some((x) => x.id === event.id)) {
      state.events.push(event);
      state.events = state.events.slice(-600);
    }
    scheduleRefresh();
  };
  state.stream.onerror = () => {
    $("workspaceInfo").textContent = "连接正在恢复 · 已保存的任务继续执行";
  };
  renderAttachments();
}
let refreshTimer;
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    refresh().catch((e) => toast(e.message));
  }, 300);
}
async function refresh() {
  if (!state.pid) return;
  const pid = state.pid;
  const [jobs, files, artifacts] = await Promise.all([
    api(base() + "/jobs"),
    api(base() + "/files"),
    api(base() + "/artifacts"),
  ]);
  if (pid !== state.pid) return;
  state.jobs = jobs;
  state.files = files;
  state.artifacts = artifacts;
  renderFeed();
  renderTree();
  updateScope();
  $("referenceHint").hidden = files.some((f) => f.kind === "corpus");
}
function parse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
function detailsText(event) {
  if (event.kind === "phase") {
    const p = parse(event.text);
    return p ? `${p.title} · ${p.detail}` : event.text;
  }
  if (event.kind === "agent") {
    const a = parse(event.text);
    if (!a) return event.text;
    if (a.type === "stream_event")
      return a.event?.delta?.text || a.event?.content_block?.text || "";
    const item = a.item || {};
    if (item.text) return item.text;
    if (item.command)
      return `$ ${item.command}\n${item.aggregated_output || ""}`;
    if (a.message?.content)
      return a.message.content
        .map(
          (x) =>
            x.text || [x.type, x.name, JSON.stringify(x.input || {})].join(" "),
        )
        .join("\n");
    if (a.type === "result")
      return a.is_error ? String(a.errors || a.result) : "Agent 返回结果";
    return a.type + (item.type ? " · " + item.type : "");
  }
  return event.text;
}
function resultText(job) {
  const data = parse(job.result);
  if (!data) return job.result || "";
  return (
    data.message ||
    data.answer ||
    (job.kind === "translate"
      ? "翻译与审校完成"
      : JSON.stringify(data, null, 2))
  );
}
function renderFeed() {
  if (!state.jobs.length) {
    $("feed").innerHTML =
      '<div class="welcome"><div class="mark">T</div><h1>让文档工作，更专注。</h1><p>上传文档，选择任务。<br>过程在这里展开，成果留在工作空间。</p></div>';
    return;
  }
  const feed = $("feed"),
    bottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
  const opened = new Set(
    [...feed.querySelectorAll("details[open]")].map((el) => el.dataset.job),
  );
  const positions = new Map(
    [...feed.querySelectorAll(".steps")].map((el) => [
      el.dataset.steps,
      el.scrollTop,
    ]),
  );
  feed.innerHTML = [...state.jobs]
    .reverse()
    .map((job) => {
      const payload = parse(job.payload) || {},
        progress = parse(job.progress) || {};
      const events = state.events
        .filter(
          (e) => e.job === job.id && !["user", "succeeded"].includes(e.kind),
        )
        .slice(-100);
      const files = state.files.filter(
        (f) => f.path.startsWith(`runs/${job.id}/`) && f.kind !== "source",
      );
      const data = parse(job.result);
      if (data?.documents)
        for (const out of data.documents) {
          const file = state.files.find((f) => f.id === out.file_id);
          if (file && !files.some((x) => x.id === file.id)) files.push(file);
        }
      const summary =
        job.state === "running"
          ? progress.title || "Agent 正在工作"
          : statuses[job.state];
      const isActive = !terminal.has(job.state);
      return `<article class="message user">${esc(payload.message || labels[job.kind])}${(payload.file_ids || []).map((id) => `<div class="hint">▤ ${esc(state.files.find((f) => f.id === id)?.name || "附件")}</div>`).join("")}</article><article class="message assistant"><div class="assistant-label">${esc(project()?.agent || "Agent")} · ${esc(labels[job.kind] || job.kind)}</div><details class="work-status ${job.state}" data-job="${job.id}" ${opened.has(job.id) ? "open" : ""}><summary><span class="dot"></span>${esc(summary)}<span class="elapsed" data-start="${job.created}" data-active="${isActive}">${isActive ? Math.max(0, Math.floor(Date.now() / 1000 - job.created)) + "s" : ""}</span></summary><div class="steps" data-steps="${job.id}">${progress.detail ? `<div class="step">${esc(progress.detail)}</div>` : ""}${events
        .filter((e) => detailsText(e))
        .map(
          (e) =>
            `<div class="step">${esc(detailsText(e).slice(0, 4000))}</div>`,
        )
        .join(
          "",
        )}${isActive ? `<button data-cancel="${job.id}">停止任务</button>` : ""}</div></details>${job.result ? `<div class="result">${esc(resultText(job))}</div>` : ""}${files.map((f) => `<button class="artifact" data-preview="${f.id}">▤ ${esc(f.name)} <span>↗</span></button>`).join("")}${job.kind === "style" && job.state === "succeeded" ? '<button class="artifact" data-config="style">▤ 翻译风格.md ↗</button><button class="artifact" data-config="terms">▤ 术语规范.json ↗</button>' : ""}${job.kind === "factcheck" && job.state === "succeeded" && state.jobs[0].id === job.id ? `<button class="primary" data-accept="${job.id}">是，生成修订副本</button>` : ""}</article>`;
    })
    .join("");
  feed.querySelectorAll("[data-cancel]").forEach(
    (el) =>
      (el.onclick = () =>
        api(base() + `/jobs/${el.dataset.cancel}/cancel`, { method: "POST" })
          .then(refresh)
          .catch((e) => toast(e.message))),
  );
  feed
    .querySelectorAll("[data-preview]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          openFile(el.dataset.preview).catch((e) => toast(e.message))),
    );
  feed
    .querySelectorAll("[data-config]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          openConfig(el.dataset.config).catch((e) => toast(e.message))),
    );
  feed.querySelectorAll("[data-accept]").forEach(
    (el) =>
      (el.onclick = async () => {
        el.disabled = true;
        try {
          await api(base() + "/messages", {
            method: "POST",
            body: JSON.stringify({
              message: "是",
              report_job: el.dataset.accept,
            }),
          });
          await refresh();
        } catch (e) {
          toast(e.message);
          el.disabled = false;
        }
      }),
  );
  for (const el of feed.querySelectorAll(".steps"))
    el.scrollTop = positions.get(el.dataset.steps) || 0;
  if (bottom) feed.scrollTop = feed.scrollHeight;
}
function renderTree() {
  const groups = [
    ["指导文件", null],
    ["参考语料", state.files.filter((f) => f.kind === "corpus")],
    [
      "上传文档",
      state.files.filter(
        (f) =>
          ["source", "original", "manuscript"].includes(f.kind) &&
          !f.path.startsWith("runs/"),
      ),
    ],
    [
      "译文与修订",
      state.files.filter((f) => ["output", "edited"].includes(f.kind)),
    ],
    ["排版文件", state.files.filter((f) => f.kind === "typeset")],
    ["核查与报告", state.files.filter((f) => f.kind === "report")],
  ];
  const closed = new Set(
    [...$("tree").querySelectorAll("details:not([open])")].map(
      (e) => e.dataset.group,
    ),
  );
  $("tree").innerHTML = groups
    .map(
      ([name, files]) =>
        `<details data-group="${name}" ${closed.has(name) ? "" : "open"}><summary>${name} ${files ? `· ${files.length}` : ""}</summary>${
          files
            ? files
                .map(
                  (f) =>
                    `<div class="file-row"><button data-file="${f.id}" title="${esc(f.path)}">▤ ${esc(f.name)}</button><button class="pick" data-pick="${f.id}" title="附加到本次消息">＋</button>${f.kind === "corpus" ? `<button class="pick" data-remove="${f.id}" title="删除参考语料">×</button>` : ""}</div>`,
                )
                .join("")
            : Object.entries(configs)
                .map(
                  ([id, label]) =>
                    `<div class="file-row"><button data-config="${id}">▤ ${label}</button></div>`,
                )
                .join("")
        }</details>`,
    )
    .join("");
  $("tree").insertAdjacentHTML(
    "beforeend",
    `<details data-group="任务记录"><summary>任务记录 · ${state.artifacts.length}</summary>${state.artifacts.map((a) => `<div class="file-row"><button data-artifact="${esc(a.path)}" title="${esc(a.path)}">▤ ${esc(a.name)} · ${esc(a.path.split("/")[1].slice(0, 6))}</button></div>`).join("")}</details>`,
  );
  $("tree")
    .querySelectorAll("[data-artifact]")
    .forEach(
      (el) =>
        (el.onclick = async () => {
          if (!previewAllowed()) return;
          try {
            const url = base() + "/artifact/" + el.dataset.artifact;
            const response = await fetch(url);
            if (!response.ok) throw new Error("无法读取任务文件");
            state.preview = {
              type: el.dataset.artifact.endsWith(".md") ? "md" : "json",
              content: await response.text(),
              name: el.dataset.artifact.split("/").pop(),
              download: url,
            };
            showPreview();
          } catch (e) {
            toast(e.message);
          }
        }),
    );
  $("tree")
    .querySelectorAll("[data-file]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          openFile(el.dataset.file).catch((e) => toast(e.message))),
    );
  $("tree")
    .querySelectorAll("[data-config]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          openConfig(el.dataset.config).catch((e) => toast(e.message))),
    );
  $("tree")
    .querySelectorAll("[data-pick]")
    .forEach(
      (el) =>
        (el.onclick = () => {
          state.selected.set(el.dataset.pick, "document");
          renderAttachments();
        }),
    );
  $("tree")
    .querySelectorAll("[data-remove]")
    .forEach(
      (el) =>
        (el.onclick = async () => {
          if (!confirm("删除这份参考语料？下次更新风格将排除它的观察。"))
            return;
          try {
            await api(base() + "/files/" + el.dataset.remove, {
              method: "DELETE",
            });
            state.selected.delete(el.dataset.remove);
            await refresh();
            renderAttachments();
          } catch (e) {
            toast(e.message);
          }
        }),
    );
}
function setTask(kind) {
  state.kind = kind;
  const template = state.templates.find((t) => t.id === kind);
  $("prompt").value = template?.prompt || "";
  $("preset").hidden = kind !== "layout";
  $("clearTask").hidden = kind === "chat";
  $("templates")
    .querySelectorAll("button")
    .forEach((el) => el.classList.toggle("active", el.dataset.kind === kind));
  updateScope();
}
function updateScope() {
  const selected = [...state.selected].filter(
    ([, role]) => role === "document",
  );
  const latest = state.files.find((f) => f.kind === "output");
  $("scope").textContent =
    !selected.length && ["layout", "factcheck"].includes(state.kind)
      ? latest
        ? `本次默认使用最新译文：${latest.name}`
        : "请上传或选择本次处理的文档"
      : "仅处理本次附加或明确选中的文件";
}
function renderAttachments() {
  $("attachments").innerHTML = [...state.selected]
    .map(
      ([id, role]) =>
        `<div class="attachment"><span title="${esc(state.files.find((f) => f.id === id)?.name)}">▤ ${esc(state.files.find((f) => f.id === id)?.name || "附件")}</span><select data-role="${id}" aria-label="附件用途"><option value="document" ${role === "document" ? "selected" : ""}>文档</option><option value="glossary" ${role === "glossary" ? "selected" : ""}>词表</option></select><button data-unpick="${id}" aria-label="移除附件">×</button></div>`,
    )
    .join("");
  $("attachments")
    .querySelectorAll("[data-role]")
    .forEach(
      (el) =>
        (el.onchange = () => {
          state.selected.set(el.dataset.role, el.value);
          updateScope();
        }),
    );
  $("attachments")
    .querySelectorAll("[data-unpick]")
    .forEach(
      (el) =>
        (el.onclick = () => {
          state.selected.delete(el.dataset.unpick);
          renderAttachments();
        }),
    );
  updateScope();
}
function previewAllowed() {
  return !state.preview?.dirty || confirm("放弃未保存的编辑？");
}
function showPreview() {
  $("app").classList.add("preview-open");
  $("preview").hidden = false;
  $("previewName").textContent = state.preview.name;
  $("historyButton").hidden = !state.preview.config;
  $("saveConfig").hidden = !state.preview.config;
  $("download").href = state.preview.download;
  renderPreview(false, false);
}
async function openFile(id) {
  if (!previewAllowed()) return;
  const pid = state.pid;
  const file = state.files.find((f) => f.id === id);
  const data = await api(base() + `/files/${id}/preview`);
  if (pid !== state.pid) return;
  state.preview = {
    ...data,
    name: file.name,
    download: base() + `/files/${id}/download`,
  };
  showPreview();
}
async function openConfig(name) {
  if (!previewAllowed()) return;
  const pid = state.pid;
  const data = await api(base() + `/config/${name}`);
  if (pid !== state.pid) return;
  state.preview = {
    ...data,
    type: ["terms", "mappings", "people"].includes(name) ? "json" : "md",
    config: name,
    name: configs[name],
    download: base() + `/config/${name}/download`,
    dirty: false,
  };
  showPreview();
}
function closePreview(force = false) {
  if (!force && !previewAllowed()) return;
  state.preview = null;
  $("preview").hidden = true;
  $("app").classList.remove("preview-open");
  $("tree").hidden = false;
}
function captureEdit() {
  const p = state.preview;
  if (!p?.config) return;
  const editor = $("configEditor");
  if (editor) p.content = editor.value;
}
function renderPreview(source, capture = true) {
  if (capture) captureEdit();
  const p = state.preview;
  if (!p) return;
  p.source = source;
  $("sourceTab").classList.toggle("active", source);
  $("renderTab").classList.toggle("active", !source);
  $("sourceTab").hidden = ["pdf", "document"].includes(p.type);
  const body = $("previewBody");
  if (p.type === "pdf") {
    body.innerHTML = `<iframe title="PDF 预览" src="${esc(p.url)}"></iframe>`;
    return;
  }
  if (p.type === "document") {
    body.innerHTML = p.paragraphs
      .map(
        (text, i) =>
          `<p class="doc-paragraph"><small>${i + 1}</small>${esc(text)}</p>`,
      )
      .join("");
    return;
  }
  if (p.config && source) {
    body.innerHTML = `<textarea id="configEditor" aria-label="编辑 ${esc(p.name)}">${esc(p.content)}</textarea>`;
    $("configEditor").oninput = () => {
      p.dirty = true;
    };
    return;
  }
  if (p.config && p.type === "json" && !source) {
    renderTermTable();
    return;
  }
  if (source) {
    body.innerHTML = `<pre>${esc(p.content)}</pre>`;
    return;
  }
  if (p.type === "md") {
    body.innerHTML = md(p.content);
    return;
  }
  if (p.type === "html") {
    body.innerHTML = '<iframe title="HTML 预览" sandbox=""></iframe>';
    body.firstChild.srcdoc = p.content;
    return;
  }
  body.innerHTML = `<pre>${esc(p.content)}</pre>`;
}
function renderTermTable() {
  const p = state.preview;
  const data = parse(p.content);
  if (!data?.rows) {
    $("previewBody").innerHTML = `<pre>${esc(p.content)}</pre>`;
    return;
  }
  const fields =
    p.config === "terms"
      ? ["term", "meaning", "usage"]
      : p.config === "mappings"
        ? ["original", "translation", "context"]
        : ["original", "translation", "aliases", "context"];
  const names = {
    term: "术语",
    meaning: "含义",
    usage: "使用规范",
    original: "原文",
    translation: "目标语言",
    context: "适用范围",
    aliases: "别名",
  };
  $("previewBody").innerHTML =
    `<div class="hint">直接编辑单元格。可从表格复制多行，在下方粘贴导入。</div><table><thead><tr>${fields.map((f) => `<th>${names[f]}</th>`).join("")}<th></th></tr></thead><tbody>${data.rows.map((row, i) => `<tr>${fields.map((f) => `<td><input data-row="${i}" data-field="${f}" aria-label="${names[f]}" value="${esc(row[f])}"></td>`).join("")}<td><button data-delete-row="${i}">×</button></td></tr>`).join("")}</tbody></table><button id="addRow">＋ 添加一行</button><details><summary>从表格粘贴导入</summary><p class="hint">列顺序：${fields.map((f) => names[f]).join("、")}，用 Tab 分隔。</p><textarea id="pasteTerms" aria-label="粘贴表格" style="min-height:100px;height:100px"></textarea><button id="importTerms">预览并合并</button></details>`;
  const save = () => {
    p.content = JSON.stringify(data, null, 2);
    p.dirty = true;
  };
  $("previewBody")
    .querySelectorAll("[data-row]")
    .forEach(
      (el) =>
        (el.oninput = () => {
          data.rows[Number(el.dataset.row)][el.dataset.field] = el.value;
          save();
        }),
    );
  $("previewBody")
    .querySelectorAll("[data-delete-row]")
    .forEach(
      (el) =>
        (el.onclick = () => {
          data.rows.splice(Number(el.dataset.deleteRow), 1);
          save();
          renderTermTable();
        }),
    );
  $("addRow").onclick = () => {
    data.rows.push({
      ...Object.fromEntries(fields.map((f) => [f, ""])),
      source: "User",
      origin: "manual",
    });
    save();
    renderTermTable();
  };
  $("importTerms").onclick = () => {
    for (const line of $("pasteTerms").value.trim().split("\n")) {
      if (!line.trim()) continue;
      const cells = line.split("\t");
      data.rows.push({
        ...Object.fromEntries(fields.map((f, i) => [f, cells[i] || ""])),
        source: "User",
        origin: "manual",
      });
    }
    save();
    renderTermTable();
  };
}
listen("newWorkspace", "click", () => $("createDialog").showModal());
document
  .querySelectorAll("[data-close]")
  .forEach((el) => (el.onclick = () => el.closest("dialog").close()));
listen("createForm", "submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  const p = await api("/api/projects", {
    method: "POST",
    body: JSON.stringify(data),
  });
  $("createDialog").close();
  e.target.reset();
  await selectProject(p.id);
});
listen("model", "change", async () => {
  if (!state.pid) return;
  const p = await api(base(), {
    method: "PATCH",
    body: JSON.stringify({ model: $("model").value }),
  });
  Object.assign(project(), p);
  toast("模型已更新，运行中任务继续使用原模型");
});
listen("settings", "click", () => {
  if (state.pid) {
    $("deleteName").value = "";
    $("settingsDialog").showModal();
  }
});
listen("deleteWorkspace", "click", async () => {
  if ($("deleteName").value !== project()?.name)
    throw new Error("请输入完整工作空间名称");
  await api(base(), {
    method: "DELETE",
    body: JSON.stringify({ name: $("deleteName").value }),
  });
  state.stream?.close();
  localStorage.removeItem("transmux-v2-workspace");
  location.reload();
});
listen("attach", "click", () => {
  if (!state.pid) throw new Error("请先创建工作空间");
  $("fileInput").click();
});
listen("fileInput", "change", async (e) => {
  if (state.busy) return;
  const pid = state.pid;
  state.busy = true;
  $("send").disabled = true;
  try {
    for (const file of e.target.files) {
      const data = new FormData();
      data.append("file", file);
      toast(`正在上传 ${file.name}`);
      const result = await api(`/api/projects/${pid}/attachments`, {
        method: "POST",
        body: data,
      });
      if (pid === state.pid) {
        state.files.unshift(result);
        state.selected.set(result.id, "document");
        renderAttachments();
      }
    }
    if (pid === state.pid) await refresh();
    toast("附件已保存至工作空间");
  } finally {
    state.busy = false;
    $("send").disabled = false;
    e.target.value = "";
  }
});
listen("pickFiles", "click", () => {
  if (!state.pid) throw new Error("请先创建工作空间");
  $("fileChoices").innerHTML =
    state.files
      .filter(
        (f) =>
          !f.path.startsWith("runs/") ||
          ["output", "edited", "report"].includes(f.kind),
      )
      .map(
        (f) =>
          `<label><input type="checkbox" data-choose="${f.id}" ${state.selected.has(f.id) ? "checked" : ""}>${esc(f.name)}</label>`,
      )
      .join("") || '<p class="hint">尚无可选文件，请先上传。</p>';
  $("fileChoices")
    .querySelectorAll("input")
    .forEach(
      (el) =>
        (el.onchange = () => {
          if (el.checked) state.selected.set(el.dataset.choose, "document");
          else state.selected.delete(el.dataset.choose);
          renderAttachments();
        }),
    );
  $("filesDialog").showModal();
});
listen("clearTask", "click", () => setTask("chat"));
listen("composer", "submit", async (e) => {
  e.preventDefault();
  if (!state.pid) throw new Error("请先创建工作空间");
  if (state.busy) return;
  state.busy = true;
  $("send").disabled = true;
  try {
    const payload = {
      kind: state.kind,
      message: $("prompt").value,
      file_ids: [...state.selected]
        .filter(([, r]) => r === "document")
        .map(([id]) => id),
      glossary_ids: [...state.selected]
        .filter(([, r]) => r === "glossary")
        .map(([id]) => id),
      template: $("preset").value,
    };
    await api(base() + "/messages", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.selected.clear();
    renderAttachments();
    setTask("chat");
    await refresh();
    $("feed").scrollTop = $("feed").scrollHeight;
  } finally {
    state.busy = false;
    $("send").disabled = false;
  }
});
listen("toggleTree", "click", () => {
  if (state.preview) $("tree").hidden = !$("tree").hidden;
  else $("pickFiles").click();
});
listen("closePreview", "click", () => closePreview());
listen("renderTab", "click", () => renderPreview(false));
listen("sourceTab", "click", () => renderPreview(true));
listen("saveConfig", "click", async () => {
  captureEdit();
  const p = state.preview;
  const data = await api(base() + `/config/${p.config}`, {
    method: "PUT",
    body: JSON.stringify({ content: p.content, revision: p.revision }),
  });
  Object.assign(p, data, { dirty: false });
  renderPreview(p.source, false);
  toast("已保存并创建历史快照");
});
listen("historyButton", "click", async () => {
  const p = state.preview;
  const rows = await api(base() + `/config/${p.config}/history`);
  $("historyList").innerHTML = rows
    .map(
      (row) =>
        `<details><summary>${new Date(row.created * 1000).toLocaleString()} · ${esc(row.source)}</summary><pre>${esc(row.content)}</pre><button data-restore="${row.id}">恢复此版本</button></details>`,
    )
    .join("");
  $("historyList")
    .querySelectorAll("[data-restore]")
    .forEach(
      (el) =>
        (el.onclick = async () => {
          try {
            const data = await api(
              base() +
                `/config/${p.config}/history/${el.dataset.restore}/restore`,
              {
                method: "POST",
                body: JSON.stringify({ revision: p.revision }),
              },
            );
            Object.assign(p, data, { dirty: false });
            $("historyDialog").close();
            renderPreview(p.source, false);
            toast("已恢复，原版本保留在历史中");
          } catch (e) {
            toast(e.message);
          }
        }),
    );
  $("historyDialog").showModal();
});
window.addEventListener("beforeunload", (e) => {
  if (state.preview?.dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
setInterval(() => {
  document
    .querySelectorAll('[data-active="true"]')
    .forEach(
      (el) =>
        (el.textContent =
          Math.max(
            0,
            Math.floor(Date.now() / 1000 - Number(el.dataset.start)),
          ) + "s"),
    );
}, 1000);
async function start() {
  state.templates = await api("/api/templates");
  $("templates").innerHTML = state.templates
    .map(
      (t) => `<button type="button" data-kind="${t.id}">${t.title} ↗</button>`,
    )
    .join("");
  $("templates")
    .querySelectorAll("button")
    .forEach((el) => (el.onclick = () => setTask(el.dataset.kind)));
  await loadProjects();
  const saved = localStorage.getItem("transmux-v2-workspace");
  if (state.projects.length)
    await selectProject(
      state.projects.some((p) => p.id === saved) ? saved : state.projects[0].id,
    );
}
start().catch((e) => toast(e.message));
