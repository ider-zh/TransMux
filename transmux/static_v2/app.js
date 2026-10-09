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
  reviews: [],
  reviewTarget: null,
  jobs: [],
  events: [],
  selected: new Map(),
  kind: "chat",
  stream: null,
  preview: null,
  templates: [],
  busy: false,
  generation: 0,
  reads: new AbortController(),
  refreshRequest: 0,
  previewRequest: 0,
  uploads: new Map(),
  usage: null,
  connected: false,
};
const labels = {
  style: "学习风格",
  glossary: "上传专有名词表",
  translate: "翻译文档",
  layout: "文档排版",
  factcheck: "事实核查",
  consistency: "一致性检查",
  factfix: "修订核查结果",
  chat: "对话",
};
const statuses = {
  queued: "排队等待",
  running: "Agent 正在工作",
  succeeded: "Agent 已完成",
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
function reportError(error) {
  if (error.name !== "AbortError") toast(error.message);
}
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
      reportError(error);
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
const libraryElement = $("agentLibrary");
async function loadProjects() {
  state.projects = await api("/api/projects");
  libraryElement.remove();
  $("workspaces").innerHTML = state.projects
    .map(
      (p) =>
        `<section class="agent-group" data-agent="${p.id}"><button data-project="${p.id}" class="${p.id === state.pid ? "active" : ""}" aria-expanded="${p.id === state.pid}">◻ &nbsp;${esc(p.name)}</button><div class="agent-children" data-library-host="${p.id}" hidden></div></section>`,
    )
    .join("");
  mountAgentLibrary();
  $("workspaces")
    .querySelectorAll("[data-project]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          selectProject(el.dataset.project).catch((e) => reportError(e))),
    );
}
function mountAgentLibrary() {
  const host = document.querySelector(`[data-library-host="${state.pid}"]`);
  document.querySelectorAll("[data-library-host]").forEach(el => el.hidden = el !== host);
  document.querySelectorAll("[data-project]").forEach(el => {
    el.classList.toggle("active", el.dataset.project === state.pid);
    el.setAttribute("aria-expanded", String(el.dataset.project === state.pid));
  });
  if (host) host.append(libraryElement);
  else $("workspaces").after(libraryElement);
  libraryElement.hidden = !host;
}
async function selectProject(pid) {
  if (state.preview?.dirty && !confirm("放弃未保存的编辑？")) return;
  $("libraryDialog").close();
  $("styleRulesDialog").close();
  const generation = ++state.generation;
  state.reads.abort();
  state.reads = new AbortController();
  clearTimeout(refreshTimer);
  refreshTimer = null;
  refreshNeeded = false;
  state.stream?.close();
  state.pid = pid;
  state.selected.clear();
  state.events = [];
  state.jobs = [];
  state.files = [];
  state.artifacts = [];
  state.reviews = [];
  state.reviewTarget = null;
  state.usage = null;
  state.connected = false;
  renderAgentStatus();
  $("consistencySource").value = "";
  state.busy = false;
  closePreview(true);
  document.querySelectorAll("dialog[open]").forEach((el) => el.close());
  $("feed").innerHTML = '<p class="hint">正在加载工作空间…</p>';
  $("tree").innerHTML = "";
  $("resources").innerHTML = "";
  $("outputs").innerHTML = "";
  $("agentLibrary").hidden = false;
  $("referenceHint").hidden = true;
  $("toast").hidden = true;
  setTask("chat");
  renderAttachments();
  renderUploads();
  localStorage.setItem("transmux-v2-workspace", pid);
  if (!project()) await loadProjects();
  if (generation !== state.generation) return;
  document
    .querySelectorAll("[data-project]")
    .forEach((el) => el.classList.toggle("active", el.dataset.project === pid));
  mountAgentLibrary();
  const p = project();
  $("workspaceName").textContent = p.name;
  const info = `${p.target_language === "en" ? "English" : "简体中文"} · 单一对话`;
  $("workspaceInfo").textContent = info;
  $("agentLabel").textContent = p.agent;
  $("model").innerHTML =
    `<option value="${esc(p.model || "")}">${esc(p.model || "Agent 默认模型")}</option>`;
  $("model").disabled = true;
  api(`/api/agents/${p.agent}/models?refresh=true`, { signal: state.reads.signal })
    .then((data) => {
      if (generation !== state.generation) return;
      const models = [
        ...new Set([...(data.models || []), ...(p.model ? [p.model] : [])]),
      ];
      $("model").innerHTML =
        '<option value="">Agent 默认模型</option>' +
        models
          .map((m) => `<option value="${esc(m)}">${esc(m)}</option>`)
          .join("");
      $("model").value = p.model || "";
      $("model").title = data.warning || "已从 Agent 获取模型列表";
      if (data.warning) toast(data.warning);
    })
    .catch(reportError)
    .finally(() => {
      if (generation === state.generation) $("model").disabled = false;
    });
  const history = api(base() + "/events?tail=true", {
    signal: state.reads.signal,
  });
  const [, events] = await Promise.all([refresh(), history]);
  if (generation !== state.generation) return;
  state.events = events;
  renderFeed();
  state.stream = new EventSource(
    base() + "/stream?after=" + (events.at(-1)?.id || 0),
  );
  state.stream.onmessage = (e) => {
    if (generation !== state.generation) return;
    const event = JSON.parse(e.data);
    if (state.events.some((x) => x.id === event.id)) return;
    state.events.push(event);
    state.events = state.events.slice(-600);
    const job = state.jobs.find((j) => j.id === event.job);
    if (job && event.kind === "phase") job.progress = event.text;
    if (event.kind === "agent" || (job && event.kind === "phase")) {
      scheduleRefresh(false);
    } else scheduleRefresh(true);
  };
  state.stream.onopen = () => {
    if (generation === state.generation) { state.connected = true; renderAgentStatus(); $("workspaceInfo").textContent = info; }
  };
  state.stream.onerror = () => {
    if (generation === state.generation) { state.connected = false; renderAgentStatus(); $("workspaceInfo").textContent = "连接正在恢复 · 已保存的任务继续执行"; }
  };
}
let refreshTimer;
let refreshNeeded = false;
function scheduleRefresh(fetchData = true) {
  refreshNeeded ||= fetchData;
  if (refreshTimer) return;
  const generation = state.generation;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    if (generation !== state.generation) return;
    const needed = refreshNeeded;
    refreshNeeded = false;
    if (needed) refresh().catch(reportError);
    else renderFeed();
  }, 300);
}
async function refresh() {
  if (!state.pid) return;
  const generation = state.generation;
  const request = ++state.refreshRequest;
  const options = { signal: state.reads.signal };
  const [jobs, files, reviews, usage] = await Promise.all([
    api(base() + "/jobs", options),
    api(base() + "/files", options),
    api(base() + "/reviews", options),
    api(base() + "/usage", options),
  ]);
  if (generation !== state.generation || request !== state.refreshRequest)
    return;
  state.jobs = jobs;
  state.files = files;
  state.reviews = reviews;
  state.usage = usage;
  if ($("settingsDialog").open) renderAgentUsage();
  renderConsistencySource();
  renderStyleChoice();
  renderFeed();
  renderTree();
  renderTaskSummary();
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
  renderAgentStatus();
  if (!state.jobs.length) {
    $("feed").innerHTML =
      '<div class="welcome"><div class="mark">T</div><h1>让文档工作，更专注。</h1><p>上传文档，选择任务。<br>过程在这里展开，成果留在工作空间。</p></div>';
    $("feed").insertAdjacentHTML("beforeend", state.reviews.map(historyReview).join(""));
    bindReviewActions($("feed"));
    renderReviewCards();
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
      const summary =
        job.state === "running"
          ? progress.title || "Agent 正在工作"
          : statuses[job.state];
      const isActive = !terminal.has(job.state);
      return `<article class="message user">${esc(payload.message || labels[job.kind])}${payload._style_name ? `<div class="hint">使用风格：${esc(payload._style_name)}</div>` : ""}${payload._glossary_name ? `<div class="hint">使用词表：${esc(payload._glossary_name)}</div>` : ""}${(payload.file_ids || []).map((id) => `<div class="hint">▤ ${esc(state.files.find((f) => f.id === id)?.name || "附件")}</div>`).join("")}</article><article class="message assistant"><div class="assistant-label">${esc(project()?.agent || "Agent")} · ${esc(labels[job.kind] || job.kind)}</div><details class="work-status ${job.state}" data-job="${job.id}" ${opened.has(job.id) ? "open" : ""}><summary><span class="dot"></span>${esc(summary)}<span class="elapsed" data-start="${job.created}" data-active="${isActive}">${isActive ? Math.max(0, Math.floor(Date.now() / 1000 - job.created)) + "s" : ""}</span></summary><div class="steps" data-steps="${job.id}">${progress.detail ? `<div class="step">${esc(progress.detail)}</div>` : ""}${events
        .filter((e) => detailsText(e))
        .map(
          (e) =>
            `<div class="step">${esc(detailsText(e).slice(0, 4000))}</div>`,
        )
        .join(
          "",
        )}${isActive ? `<button data-cancel="${job.id}">停止任务</button>` : ""}</div></details>${job.result ? `<div class="result">${esc(resultText(job))}</div>` : ""}${terminal.has(job.state) ? usageLine(job.id) : ""}${state.reviews.filter(r => r.job === job.id).map(historyReview).join("")}${job.kind === "factcheck" && job.state === "succeeded" && state.jobs[0].id === job.id ? `<button class="primary" data-accept="${job.id}">是，生成修订副本</button>` : ""}</article>`;
    })
    .join("");
  feed.querySelectorAll("[data-cancel]").forEach(
    (el) =>
      (el.onclick = () =>
        api(base() + `/jobs/${el.dataset.cancel}/cancel`, { method: "POST" })
          .then(refresh)
          .catch((e) => reportError(e))),
  );
  feed
    .querySelectorAll("[data-preview]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          openFile(el.dataset.preview).catch((e) => reportError(e))),
    );
  feed
    .querySelectorAll("[data-config]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          openConfig(el.dataset.config).catch((e) => reportError(e))),
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
  feed.insertAdjacentHTML("beforeend", state.reviews.filter(r => !state.jobs.some(j => j.id === r.job)).map(historyReview).join(""));
  bindReviewActions(feed);
  for (const el of feed.querySelectorAll(".steps"))
    el.scrollTop = positions.get(el.dataset.steps) || 0;
  renderReviewCards();
  if (bottom) feed.scrollTop = feed.scrollHeight;
}
function renderGlossaryChoice() {
  const choices = state.reviews.filter(
    (r) =>
      r.kind === "glossary" &&
      r.status === "approved" &&
      !state.reviews.some(
        (n) =>
          n.root === r.root && n.status === "approved" && n.version > r.version,
      ),
  );
  const previous = $("glossaryChoice").value;
  $("glossaryChoiceLabel").hidden =
    state.kind !== "translate" || !choices.length;
  $("glossaryChoice").innerHTML = choices.length
    ? (choices.length > 1
        ? '<option value="">请选择已审核对照词表</option>'
        : "") +
      choices
        .map(
          (r) =>
            `<option value="${r.id}">${esc(r.name)} · v${r.version}</option>`,
        )
        .join("")
    : '<option value="">不使用对照词表</option>';
  if (choices.some((r) => r.id === previous))
    $("glossaryChoice").value = previous;
  $("glossaryChoice").disabled = choices.length <= 1;
}
function renderStyleChoice() {
  renderGlossaryChoice();
  const choices = state.reviews.filter(
    (r) =>
      r.kind === "style" &&
      r.status === "approved" &&
      !state.reviews.some(
        (n) =>
          n.root === r.root && n.status === "approved" && n.version > r.version,
      ),
  );
  const previous = $("styleChoice").value;
  $("styleChoiceLabel").hidden = state.kind !== "translate";
  $("styleChoice").innerHTML = choices.length
    ? (choices.length > 1 ? '<option value="">请选择已审核风格</option>' : "") +
      choices
        .map(
          (r) =>
            `<option value="${r.id}">${esc(r.name)} · v${r.version}</option>`,
        )
        .join("")
    : '<option value="generic">通用翻译风格（系统默认，未经语料学习）</option>';
  if (choices.some((r) => r.id === previous)) $("styleChoice").value = previous;
  $("styleChoice").disabled = choices.length <= 1;
}
const reviewKinds = {style: "学习风格产出", translation: "翻译产出", layout: "排版产出", layout_report: "排版与引文处理报告", consistency: "一致性核查报告", glossary: "专有名词表", report: "其他报告"};
const reviewStatuses = {pending: "未审核", approved: "已通过", ignored: "已忽略"};
function historyReview(r) {
  const name = `${r.name} · v${r.version}`;
  return `<section class="history-review ${r.status}" data-history-review="${r.id}" aria-label="${esc(reviewKinds[r.kind])}：${esc(name)}"><span class="review-state">${esc(reviewStatuses[r.status])}</span><span class="review-filename" title="${esc(name)}">${esc(name)}</span><div class="review-actions"><button data-review-preview="${r.file_id}">预览</button>${r.status === "pending" ? `<button class="primary" data-review-approve="${r.id}">${r.kind === "style" ? "逐条审核风格" : r.kind === "translation" ? "原文对照审核" : "审核通过"}</button><button data-review-ignore="${r.id}">忽略</button>${["style", "glossary", "translation"].includes(r.kind) ? `<button data-review-edit="${r.id}">提出修改</button>` : ""}` : r.status === "ignored" ? `<button data-review-restore="${r.id}">恢复审核</button>` : ""}<button data-review-attach="${r.file_id}">附加到本次任务</button></div></section>`;
}
function bindReviewActions(root) {
  root.querySelectorAll('[data-review-attach]').forEach(el => el.onclick = () => {
    state.selected.set(el.dataset.reviewAttach, 'document');
    renderAttachments();
    showTaskWarning('');
    toast('已附加到本次任务，请选择任务模板');
  });
  root.querySelectorAll("[data-review-preview]").forEach(el => el.onclick = () => openFile(el.dataset.reviewPreview).catch(reportError));
  root.querySelectorAll("[data-review-edit]").forEach(el => el.onclick = () => selectReview(el.dataset.reviewEdit));
  for (const action of ["approve", "ignore", "restore"]) root.querySelectorAll(`[data-review-${action}]`).forEach(el => el.onclick = async () => {
    if (action === "approve" && state.reviews.find(r => r.id === el.dataset.reviewApprove)?.kind === "style") {
      openStyleRules(el.dataset.reviewApprove).catch(reportError);
      return;
    }
    if (action === "approve" && state.reviews.find(r => r.id === el.dataset.reviewApprove)?.kind === "translation") {
      const review = state.reviews.find(r => r.id === el.dataset.reviewApprove);
      openFile(review.file_id).catch(reportError);
      return;
    }
    const generation = state.generation;
    el.disabled = true;
    try {
      await api(base() + `/reviews/${el.getAttribute(`data-review-${action}`)}/${action}`, {method: "POST", body: "{}"});
      if (generation !== state.generation) return;
      await refresh();
      toast({approve: "该版本已人工审核通过", ignore: "已忽略，文件和历史记录保留", restore: "已恢复到待审核"}[action]);
    } catch (e) { reportError(e); el.disabled = false; }
  });
}
function renderReviewCards() {
  const box = $("outputs");
  box.innerHTML = ["pending", "approved", "ignored"].map(status => {
    const rows = state.reviews.filter(r => r.status === status);
    if (!rows.length) return "";
    const groups = Object.entries(reviewKinds).map(([kind, title]) => {
      const entries = rows.filter(r => r.kind === kind);
      if (!entries.length) return "";
      return `<section class="output-group" data-output-kind="${kind}"><h3>${title}<span>${entries.length}</span></h3>${entries.map(r => `<article class="review-card" data-output="${r.file_id}"><strong class="output-filename" title="${esc(r.name)} · v${r.version} · ${reviewStatuses[r.status]}">${esc(r.name)} <small>· v${r.version}</small></strong><div class="output-actions"><button data-review-preview="${r.file_id}">预览</button><button data-review-attach="${r.file_id}">附加到本次任务</button><a href="${base()}/files/${r.file_id}/download" download>下载</a>${status === "pending" ? `<button data-review-jump="${r.id}">前往审核</button>` : status === "ignored" ? `<button data-review-restore="${r.id}">恢复审核</button>` : ""}</div></article>`).join("")}</section>`;
    }).join("");
    return status === "ignored" ? `<details class="output-status ignored" data-output-status="ignored"><summary>已忽略 · ${rows.length}</summary>${groups}</details>` : `<section class="output-status ${status}" data-output-status="${status}"><h2>${status === "pending" ? "未审核" : "已通过"}<span>${rows.length}</span></h2>${groups}</section>`;
  }).join("") || '<div class="empty">任务完成后，产出会显示在这里</div>';
  bindReviewActions(box);
  box.querySelectorAll("[data-review-jump]").forEach(el => el.onclick = () => {
    const card = document.querySelector(`[data-history-review="${el.dataset.reviewJump}"]`);
    card?.scrollIntoView({behavior: "smooth", block: "center"});
    card?.querySelector("button")?.focus({preventScroll: true});
  });
}
function renderAgentStatus() {
  const running = state.jobs.filter(j => j.state === "running");
  const queued = state.jobs.filter(j => j.state === "queued");
  const pending = state.reviews.filter(r => r.status === "pending");
  let status = "idle", label = "空闲";
  if (!state.pid) label = "未选择 Agent";
  else if (!state.connected) { status = "connecting"; label = "连接中"; }
  else if (running.length) { status = "running"; const job = running[0], progress = parse(job.progress) || {};
    const phases = {translate: '翻译中', style: '学习风格中', layout: '排版中', consistency: '一致性检查中', glossary: '提取专有名词中', chat: '处理中', revise: '修订中'};
    label = `${progress.title || phases[job.kind] || '处理中'}${queued.length ? ` · 排队 ${queued.length}` : ''}`; }
  else if (queued.length) { status = "running"; label = `排队 ${queued.length}`; }
  else if (pending.length) { status = "review"; label = `待审核 ${pending.length}`; }
  else if (["failed", "needs_attention", "interrupted"].includes(state.jobs[0]?.state)) { status = "attention"; label = "需处理"; }
  $("agentStatus").dataset.state = status;
  const progress = running.length ? parse(running[0].progress) || {} : {};
  const detail = running.length ? (progress.detail || 'Agent 正在执行任务') : '';
  $('agentStatus').innerHTML = `<span class="status-copy"><strong>${esc(label)}</strong>${detail ? `<small>${esc(detail)}</small>` : ''}</span>`;
  $("agentStatus").title = running.length ? (parse(running[0].progress)?.title || "Agent 正在执行任务") : label;
}
const number = value => Number(value || 0).toLocaleString("zh-CN");
const money = value => `¥${Number(value || 0).toFixed(6)}`;
function usageLine(jid) {
  const u = state.usage?.jobs[jid];
  if (!u || (!u.reported_calls && (u.calls || !u.tracking))) return '<div class="task-usage">Token 用量未记录，暂无费用估算</div>';
  const partial = u.incomplete_calls || !u.tracking;
  return `<div class="task-usage" data-usage-job="${jid}"><strong>${number(u.total_tokens)} Token · 预估 ${money(u.estimated_cny)}</strong><span>输入 ${number(u.input_tokens)}（缓存 ${number(u.cached_input_tokens)}） · 输出 ${number(u.output_tokens)} · Kimi K3 标准${partial ? " · 用量不完整，仅统计已报告部分" : ""}</span></div>`;
}
function renderAgentUsage() {
  const u = state.usage?.total, p = state.usage?.pricing;
  if (!u || !p) { $("agentUsage").textContent = "正在读取用量…"; return; }
  $("agentUsage").innerHTML = `<h3>累计用量 · ${esc(project()?.name)}</h3><div class="usage-totals"><strong>${number(u.total_tokens)}<small>Token</small></strong><strong>${money(u.estimated_cny)}<small>统一费用预估</small></strong></div><p>输入 ${number(u.input_tokens)} · 其中缓存 ${number(u.cached_input_tokens)}<br>输出 ${number(u.output_tokens)} · ${number(u.calls)} 次引擎调用</p>${u.incomplete_calls || u.untracked_jobs ? '<p class="usage-warning">部分调用或历史任务未提供完整用量，以上仅累计已报告部分。</p>' : ''}<p class="hint">Kimi K3 / 每百万 Token：输入 ¥${p.input}、缓存命中 ¥${p.cached_input}、输出 ¥${p.output}。${esc(p.note)}<br><a href="${esc(p.source)}" target="_blank" rel="noopener noreferrer">官方价格</a> · 核对于 ${esc(p.checked_on)}</p>`;
}
function selectReview(id) {
  const row = state.reviews.find((r) => r.id === id);
  if (!row) return;
  setTask("chat");
  state.selected.clear();
  state.reviewTarget = id;
  if (row.kind === "translation") state.selected.set(row.file_id, "document");
  renderAttachments();
  $("reviewTarget").hidden = false;
  $("reviewTarget").textContent =
    `修改对象：${row.name} · v${row.version}（修改后生成待审核新版本）`;
  $("prompt").focus();
}
function fileTitle(file) {
  const review = state.reviews.find((r) => r.file_id === file.id);
  return (
    file.name +
    (review
      ? ` · v${review.version} · ${reviewStatuses[review.status]}`
      : "")
  );
}
function renderTree() {
  const previousGroups = new Set([...$("tree").querySelectorAll("details")].map(el => el.dataset.group));
  const closed = new Set([...$("tree").querySelectorAll("details:not([open])")].map(el => el.dataset.group));
  const root = {folders: new Map(), files: []};
  for (const file of state.files) {
    let node = root;
    const parts = file.path.split("/");
    parts.pop();
    for (const part of parts) {
      if (!node.folders.has(part)) node.folders.set(part, {folders: new Map(), files: []});
      node = node.folders.get(part);
    }
    node.files.push(file);
  }
  const fileRow = f => `<div class="file-row"><button data-file="${f.id}" title="${esc(f.path)}">▤ ${esc(fileTitle(f))}</button><button class="pick" data-pick="${f.id}" title="附加到本次任务">＋</button>${f.kind === "corpus" ? `<button class="pick" data-remove="${f.id}" title="删除参考语料">×</button>` : ""}</div>`;
  const folderLabels = {sources: "sources · 上传文档", corpus: "corpus · 参考文档", runs: "runs · 任务文件", outputs: "outputs · 译文", styles: "styles · 翻译风格", glossaries: "glossaries · 专有名词"};
  function folder(node, prefix = "") {
    return [...node.folders].sort(([a], [b]) => a.localeCompare(b)).map(([name, child]) => {
      const path = prefix + name;
      const label = folderLabels[name] || (/^[a-f0-9]{32}$/.test(name) ? name.slice(0, 8) + "…" : name);
      return `<details data-group="${esc(path)}" ${closed.has(path) || (!previousGroups.has(path) && !prefix && name === "runs") ? "" : "open"}><summary title="${esc(path)}">${esc(label)}</summary>${folder(child, path + "/")}</details>`;
    }).join("") + node.files.map(fileRow).join("");
  }
  $("tree").innerHTML = folder(root) || '<p class="empty">文档库为空，请先上传文件</p>';
  $("libraryCount").textContent = state.files.length;
  $("resources").innerHTML = `<details open><summary>翻译风格</summary>${state.files.filter(f => f.kind === "style").map(fileRow).join("") || '<p class="empty">通过“学习”积累翻译风格</p>'}<div class="file-row"><button data-config="requirements">用户要求.md</button></div></details><details open><summary>专有名词</summary>${state.files.filter(f => f.kind === "glossary").map(fileRow).join("") || '<p class="empty">上传专有名词表以积累翻译对照</p>'}<details><summary>已有术语与人名</summary>${Object.entries(configs).filter(([id]) => ["terms", "mappings", "people"].includes(id)).map(([id, label]) => `<div class="file-row"><button data-config="${id}">${label}</button></div>`).join("")}</details></details>`;
  $("tree").insertAdjacentHTML(
    "beforeend",
    `<details data-group="任务记录"><summary>任务记录 · 点击加载</summary>${state.artifacts.map((a) => `<div class="file-row"><button data-artifact="${esc(a.path)}" title="${esc(a.path)}">▤ ${esc(a.name)} · ${esc(a.path.split("/")[1].slice(0, 6))}</button></div>`).join("")}</details>`,
  );
  const records = $("tree").querySelector('[data-group="任务记录"]');
  records.ontoggle = async () => {
    if (!records.open || records.dataset.loaded) return;
    records.dataset.loaded = "true";
    const generation = state.generation;
    try {
      const artifacts = await api(base() + "/artifacts", {
        signal: state.reads.signal,
      });
      if (generation !== state.generation || !records.isConnected) return;
      records.querySelector("summary").textContent =
        `任务记录 · ${artifacts.length}`;
      records.querySelectorAll(".file-row").forEach((el) => el.remove());
      for (const a of artifacts) {
        const row = document.createElement("div");
        row.className = "file-row";
        const button = document.createElement("button");
        button.textContent = a.path;
        button.onclick = async () => {
          if (!previewAllowed()) return;
          try {
            const request = ++state.previewRequest;
            const url = base() + "/artifact/" + a.path;
            const response = await fetch(url, { signal: state.reads.signal });
            if (!response.ok) throw new Error("无法读取任务文件");
            const content = await response.text();
            if (
              generation !== state.generation ||
              request !== state.previewRequest
            )
              return;
            state.preview = {
              type: a.path.endsWith(".md") ? "md" : "json",
              content,
              name: a.name,
              download: url,
            };
            showPreview();
          } catch (e) {
            reportError(e);
          }
        };
        row.append(button);
        records.append(row);
      }
    } catch (e) {
      delete records.dataset.loaded;
      reportError(e);
    }
  };
  $("libraryDialog")
    .querySelectorAll("[data-file]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          openFile(el.dataset.file).catch((e) => reportError(e))),
    );
  $("libraryDialog")
    .querySelectorAll("[data-config]")
    .forEach(
      (el) =>
        (el.onclick = () =>
          openConfig(el.dataset.config).catch((e) => reportError(e))),
    );
  $("libraryDialog")
    .querySelectorAll("[data-pick]")
    .forEach(
      (el) =>
        (el.onclick = () => {
          state.selected.set(el.dataset.pick, "document");
          renderAttachments();
        }),
    );
  $("libraryDialog")
    .querySelectorAll("[data-remove]")
    .forEach(
      (el) =>
        (el.onclick = async () => {
          if (!confirm("删除这份参考语料？下次更新风格将排除它的观察。"))
            return;
          try {
            const generation = state.generation;
            await api(base() + "/files/" + el.dataset.remove, {
              method: "DELETE",
            });
            if (generation !== state.generation) return;
            state.selected.delete(el.dataset.remove);
            await refresh();
            renderAttachments();
          } catch (e) {
            toast(e.message);
          }
        }),
    );
}
function renderConsistencySource() {
  const previousSource = $("consistencySource").value;
  $("consistencySource").innerHTML = '<option value="">自动使用项目译文的原文</option>' + state.files.filter(f => ["source", "corpus", "original", "manuscript"].includes(f.kind) && !f.path.startsWith("runs/")).map(f => `<option value="${f.id}">${esc(f.name)}</option>`).join("");
  if (state.files.some(f => f.id === previousSource)) $("consistencySource").value = previousSource;
}
function showTaskWarning(message) {
  $('taskWarning').textContent = message ? '⚠ ' + message : '';
  $('taskWarning').hidden = !message;
}
function validateTask() {
  if (!state.pid) throw new Error('请先创建或选择 Agent');
  if (state.uploads.get(state.pid)?.active) throw new Error('文件仍在上传或解析，请完成后选择任务');
  if (state.kind === 'chat') return;
  let ids = [...state.selected].filter(([, role]) => state.kind === 'glossary' || role === 'document').map(([id]) => id);
  if (!ids.length) throw new Error(`“${labels[state.kind]}”需要文档，请先上传文件或从文档库附加`);
  if (ids.some(id => !state.files.some(f => f.id === id))) throw new Error('所选文件已不存在，请重新从文档库附加');
  if (['layout', 'consistency', 'glossary'].includes(state.kind) && ids.length !== 1) throw new Error('本次任务请选择一份文档');
  if (state.kind === 'layout' && ids.some(id => {
    const file = state.files.find(f => f.id === id), review = state.reviews.find(r => r.file_id === id);
    return (review || ['output', 'edited', 'style', 'glossary'].includes(file.kind)) && review?.status !== 'approved';
  })) throw new Error('请先审核通过所选产出，再进行排版');
}
async function launchTemplate(kind) {
  if (state.busy) return;
  setTask(kind);
  try {
    validateTask();
    showTaskWarning('');
    if (['translate', 'layout', 'consistency'].includes(kind)) openTaskOptions();
    else await submitTask();
  } catch (e) { showTaskWarning(e.message); }
}
function setTask(kind) {
  showTaskWarning("");
  state.kind = kind;
  state.reviewTarget = null;
  $("reviewTarget").hidden = true;
  renderStyleChoice();
  const template = state.templates.find((t) => t.id === kind);
  $("prompt").value = template?.prompt || "";
  $("presetLabel").hidden = kind !== "layout";
  $("consistencySourceLabel").hidden = kind !== "consistency";
  renderConsistencySource();
  $("taskOptions").hidden = !["layout", "translate", "consistency"].includes(kind);
  renderTaskSummary();
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
  $("scope").textContent = !selected.length
    ? "请上传文件，或从文档库、产出和对话历史附加本次处理的文件"
    : "仅处理本次附加的文件 · 提交后附件区清空";
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
  if (!$("previewDialog").open) $("previewDialog").showModal();
  $("preview").hidden = false;
  $("previewName").textContent = state.preview.name;
  $("historyButton").hidden = !state.preview.config;
  $("saveConfig").hidden = !state.preview.config;
  $("download").href = state.preview.download;
  const review = state.reviews.find(
    (r) => state.preview.download === base() + `/files/${r.file_id}/download`,
  );
  $("reviseVersion").hidden = !review || !["style", "glossary", "translation"].includes(review.kind);
  $("reviseVersion").onclick = () => { if (review) { closePreview(); selectReview(review.id); } };
  $("translationReviewActions").hidden = state.preview.type !== "comparison" || review?.status !== "pending";
  $("approveTranslation").disabled = !state.preview.paragraphs?.length;
  for (const [id, action] of [["approveTranslation", "approve"], ["ignoreTranslation", "ignore"]]) {
    $(id).onclick = async () => {
      const preview = state.preview, generation = state.generation, url = base();
      $("approveTranslation").disabled = true;
      $("ignoreTranslation").disabled = true;
      try {
        await api(url + `/reviews/${review.id}/${action}`, {method: "POST", body: "{}"});
        if (generation !== state.generation) return;
        if (state.preview === preview) closePreview();
        await refresh();
        toast(action === "approve" ? "译文已人工审核通过" : "已忽略，文件和历史记录保留");
      } catch (e) { reportError(e); }
      finally {
        if (state.preview === preview) {
          $("approveTranslation").disabled = !preview.paragraphs?.length;
          $("ignoreTranslation").disabled = false;
        }
      }
    };
  }
  $("ignoreTranslation").disabled = false;
  renderPreview(false, false);
}
async function openFile(id) {
  if (!previewAllowed()) return;
  const generation = state.generation;
  const request = ++state.previewRequest;
  const file = state.files.find((f) => f.id === id);
  const review = state.reviews.find(r => r.file_id === id);
  let data;
  if (review?.kind === "translation") {
    try {
      data = {...await api(base() + `/files/${id}/comparison`), type: "comparison"};
    } catch (e) {
      data = {type: "comparison", paragraphs: [], error: e.message};
    }
  } else data = await api(base() + `/files/${id}/preview`);
  if (generation !== state.generation || request !== state.previewRequest)
    return;
  state.preview = {
    ...data,
    glossary: file.kind === "glossary",
    name:
      file.name +
      (state.reviews.find((r) => r.file_id === id)?.status === "pending"
        ? " · 待审核草稿"
        : ""),
    download: base() + `/files/${id}/download`,
  };
  showPreview();
}
async function openConfig(name) {
  if (!previewAllowed()) return;
  const generation = state.generation;
  const request = ++state.previewRequest;
  const data = await api(base() + `/config/${name}`);
  if (generation !== state.generation || request !== state.previewRequest)
    return;
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
  state.previewRequest++;
  state.preview = null;
  $("previewBody").replaceChildren();
  $("preview").hidden = true;
  $("previewDialog").close();
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
  $("sourceTab").hidden = ["pdf", "document", "comparison"].includes(p.type);
  const body = $("previewBody");
  $("renderTab").textContent = p.type === "comparison" ? "原文对照" : "预览";
  if (p.type === "comparison") {
    if (!p.paragraphs.length) {
      body.innerHTML = `<p role="alert">暂时无法展示原文对照：${esc(p.error || "缺少段落对应记录")}。请下载核对；当前窗口暂不能审核通过。</p>`;
      return;
    }
    const texts = (values, fallback) => (values || [fallback || ""]).map(text => `<p class="doc-paragraph">${esc(text)}</p>`).join("");
    body.innerHTML = `<div class="comparison-heading"><strong>原文 · ${esc(p.source_name)}</strong><strong>译文 · 当前版本</strong></div>` +
      p.paragraphs.map((row, i) => `<section class="comparison-group" aria-label="第 ${i + 1} 组"><div class="comparison-source"><small>原文 · 第 ${i + 1} 组</small>${texts(row.original_paragraphs, row.original)}</div><div class="comparison-target"><small>译文 · 第 ${i + 1} 组</small>${texts(row.translations, row.translation)}</div>${row.reason ? `<p class="comparison-reason">段落调整：${esc(row.reason)}</p>` : ""}</section>`).join("");
    return;
  }
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
    if (p.glossary) {
      const decode = (value) => {
        const el = document.createElement("textarea");
        el.innerHTML = value.replace(/<br>/g, "\n");
        return esc(el.value).replace(/\n/g, "<br>");
      };
      const rows = p.content
        .split("\n")
        .filter((line) => line.startsWith("|"))
        .slice(2);
      body.innerHTML =
        '<h2>Translation Glossary</h2><table class="glossary-table"><thead><tr><th>原文</th><th>译文</th><th>适用语境</th></tr></thead><tbody>' +
        rows
          .map(
            (line) =>
              "<tr>" +
              line
                .split("|")
                .slice(1, -1)
                .map((cell) => "<td>" + decode(cell.trim()) + "</td>")
                .join("") +
              "</tr>",
          )
          .join("") +
        "</tbody></table>";
      return;
    }
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
let modelRequest = 0;
let loadingCreateModels = false;
async function loadCreateModels(reset = false) {
  const request = ++modelRequest;
  const agent = $("createAgent").value;
  const previous = reset ? "" : $("createModel").value;
  loadingCreateModels = true;
  $("createModel").disabled = true;
  $("refreshCreateModels").disabled = true;
  $("createForm").querySelector(".primary").disabled = true;
  $("createModelsStatus").textContent = "正在从 Agent 获取模型…";
  $("createModel").innerHTML = '<option value="">沿用 Agent 默认模型</option>';
  try {
    const data = await api(`/api/agents/${agent}/models?refresh=true`);
    if (request !== modelRequest || agent !== $("createAgent").value) return;
    const entries = new Map((data.entries || []).map((m) => [m.id, m.name]));
    $("createModel").innerHTML += (data.models || [])
      .map(
        (id) =>
          `<option value="${esc(id)}">${esc(entries.get(id) && entries.get(id) !== id ? `${entries.get(id)} · ${id}` : id)}</option>`,
      )
      .join("");
    $("createModel").value = (data.models || []).includes(previous)
      ? previous
      : (data.models || [])[0] || "";
    $("createModelsStatus").textContent =
      data.warning || `已从 Agent 获取 ${(data.models || []).length} 个模型`;
  } catch (error) {
    if (request === modelRequest)
      $("createModelsStatus").textContent =
        "获取失败，可重试或沿用 Agent 默认模型";
  } finally {
    if (request === modelRequest) {
      loadingCreateModels = false;
      $("createModel").disabled = false;
      $("refreshCreateModels").disabled = false;
      $("createForm").querySelector(".primary").disabled = false;
    }
  }
}
let recommendedName = "";
function suggestAgentName() {
  const input = $("createForm").elements.name;
  if (input.value && input.value !== recommendedName) return;
  const language = $("createForm").elements.target_language.value === "en" ? "英译" : "中译";
  const engine = $("createAgent").selectedOptions[0].textContent;
  const baseName = `${engine} · ${language}助手`;
  let name = baseName, n = 2;
  while (state.projects.some(p => p.name === name)) name = `${baseName} ${n++}`;
  input.value = recommendedName = name;
}
listen("newWorkspace", "click", () => {
  suggestAgentName();
  $("createDialog").showModal();
  return loadCreateModels(true);
});
listen("createAgent", "change", () => { suggestAgentName(); return loadCreateModels(true); });
$("createForm").elements.target_language.addEventListener("change", suggestAgentName);
listen("refreshCreateModels", "click", () => loadCreateModels());
document
  .querySelectorAll("[data-close]")
  .forEach((el) => (el.onclick = () => el.closest("dialog").close()));
listen("createForm", "submit", async (e) => {
  e.preventDefault();
  if (loadingCreateModels) return;
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
  const current = project();
  const generation = state.generation;
  const p = await api(base(), {
    method: "PATCH",
    body: JSON.stringify({ model: $("model").value }),
  });
  Object.assign(current, p);
  if (generation !== state.generation) return;
  toast("模型已更新，运行中任务继续使用原模型");
});
listen("settings", "click", async () => {
  if (state.pid) {
    $("deleteName").value = "";
    $("settingsDialog").showModal();
    renderAgentUsage();
    const generation = state.generation;
    const usage = await api(base() + "/usage");
    if (generation === state.generation) { state.usage = usage; renderAgentUsage(); }
  }
});
listen("deleteWorkspace", "click", async () => {
  if ($("deleteName").value !== project()?.name)
    throw new Error("请输入完整Agent 名称");
  await api(base(), {
    method: "DELETE",
    body: JSON.stringify({ name: $("deleteName").value }),
  });
  state.stream?.close();
  localStorage.removeItem("transmux-v2-workspace");
  location.reload();
});
listen("attach", "click", () => {
  if (!state.pid) throw new Error("请先创建 Agent");
  $("fileInput").click();
});
function renderUploads() {
  const batch = state.uploads.get(state.pid);
  $("send").disabled = state.busy || !!batch?.active;
  $("attach").disabled = !!batch?.active;
  $("uploadProgress").hidden = !batch;
  $("uploadProgress").innerHTML = (batch?.items || [])
    .map(
      (item) =>
        `<div class="upload-row"><span>${esc(item.name)}</span><span>${esc(item.status)}</span><progress aria-label="${esc(item.name)} 上传进度" max="100" ${item.percent === null ? "" : `value="${item.percent}"`}></progress></div>`,
    )
    .join("");
}
function uploadFile(pid, file, item) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/projects/${pid}/attachments`);
    const paint = () => {
      if (pid === state.pid) renderUploads();
    };
    xhr.upload.onprogress = (e) => {
      item.percent = e.lengthComputable
        ? Math.floor((e.loaded / e.total) * 100)
        : null;
      item.status =
        item.percent === null ? "正在上传" : `正在上传 ${item.percent}%`;
      paint();
    };
    xhr.upload.onload = () => {
      item.percent = null;
      item.status = "上传完成，正在解析文档…";
      paint();
    };
    xhr.onload = () => {
      let data;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        reject(new Error("服务器返回了无法读取的响应"));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else
        reject(
          new Error(typeof data.detail === "string" ? data.detail : "上传失败"),
        );
    };
    xhr.onerror = () => reject(new Error("网络中断，请检查文件区后重试"));
    xhr.onabort = () => reject(new Error("上传已取消"));
    const data = new FormData();
    data.append("file", file);
    xhr.send(data);
  });
}
listen("fileInput", "change", async (e) => {
  const pid = state.pid,
    generation = state.generation;
  const files = Array.from(e.target.files);
  e.target.value = "";
  if (!pid || state.uploads.get(pid)?.active || !files.length) return;
  const batch = {
    active: true,
    items: files.map((f) => ({ name: f.name, status: "等待上传", percent: 0 })),
  };
  state.uploads.set(pid, batch);
  renderUploads();
  try {
    for (const [i, file] of files.entries()) {
      const item = batch.items[i];
      try {
        const result = await uploadFile(pid, file, item);
        item.status = "已保存";
        item.percent = 100;
        if (generation === state.generation) {
          state.files.unshift(result);
          state.selected.set(result.id, "document");
          renderAttachments();
        }
      } catch (error) {
        item.status = `失败：${error.message}`;
        item.percent = 0;
      }
      if (pid === state.pid) renderUploads();
    }
    if (pid === state.pid) await refresh();
  } finally {
    batch.active = false;
    if (pid === state.pid) renderUploads();
  }
});
listen("pickFiles", "click", () => {
  if (!state.pid) throw new Error("请先创建 Agent");
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
async function submitTask() {
  if (!state.pid) throw new Error("请先创建 Agent");
  if (state.busy) return;
  validateTask();
  showTaskWarning("");
  const generation = state.generation;
  state.busy = true;
  $("send").disabled = true;
  try {
    const payload = {
      kind: state.kind,
      review_id: state.reviewTarget,
      consistency_source_id: state.kind === "consistency" ? $("consistencySource").value || null : null,
      glossary_version_id:
        state.kind === "translate" ? $("glossaryChoice").value || null : null,
      style_version_id:
        state.kind === "translate" ? $("styleChoice").value || null : null,
      message: $("prompt").value,
      file_ids: [...state.selected]
        .filter(([, r]) => state.kind === "glossary" || r === "document")
        .map(([id]) => id),
      glossary_ids: [...state.selected]
        .filter(([, r]) => state.kind !== "glossary" && r === "glossary")
        .map(([id]) => id),
      template: $("preset").value,
    };
    await api(base() + "/messages", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (generation !== state.generation) return;
    state.selected.clear();
    state.uploads.delete(state.pid);
    renderUploads();
    $("consistencySource").value = "";
    renderAttachments();
    setTask("chat");
    await refresh();
    $("feed").scrollTop = $("feed").scrollHeight;
  } finally {
    if (generation === state.generation) {
      state.busy = false;
      renderUploads();
    }
  }
}
listen("composer", "submit", async e => { e.preventDefault(); try { await submitTask(); } catch (e) { showTaskWarning(e.message); } });
listen("toggleTree", "click", refresh);
listen("closePreview", "click", () => closePreview());
listen("previewDialog", "cancel", e => { e.preventDefault(); closePreview(); });

let taskOptionSnapshot;
function renderTaskSummary() {
  const selections = state.kind === "layout" ? [$("preset").selectedOptions[0]?.textContent] : state.kind === "translate" ? [$("styleChoice").selectedOptions[0]?.textContent, $("glossaryChoice").selectedOptions[0]?.textContent] : state.kind === "consistency" ? [$("consistencySource").selectedOptions[0]?.textContent] : [];
  $("taskSummary").textContent = selections.filter(Boolean).join(" · ");
  $("taskSummary").hidden = !selections.length;
}
function openTaskOptions() {
  taskOptionSnapshot = [$("preset").value, $("styleChoice").value, $("glossaryChoice").value, $("consistencySource").value];
  $("taskDialogTitle").textContent = labels[state.kind] + " · 任务选项";
  $("taskDialogHint").textContent = state.kind === "layout" ? "选择排版样式，结果将作为未审核产出保存。" : state.kind === "consistency" ? "附加一份待检查译文。项目译文自动关联原文；外部译文请从文档库选择原文。仅生成报告，不改写文档。" : "选择已审核的翻译资源，用于本次翻译。";
  $("taskDialog").showModal();
}
listen("taskOptions", "click", openTaskOptions);
listen("confirmTaskOptions", "click", async () => { renderTaskSummary(); $("taskDialog").close(); try { await submitTask(); } catch (e) { showTaskWarning(e.message); } });
function cancelTaskOptions() {
  if (taskOptionSnapshot) ["preset", "styleChoice", "glossaryChoice", "consistencySource"].forEach((id, i) => $(id).value = taskOptionSnapshot[i]);
  renderTaskSummary(); $("taskDialog").close();
}
listen("cancelTaskOptions", "click", cancelTaskOptions);
listen("taskDialog", "cancel", e => { e.preventDefault(); cancelTaskOptions(); });
listen("renderTab", "click", () => renderPreview(false));
listen("sourceTab", "click", () => renderPreview(true));
listen("saveConfig", "click", async () => {
  captureEdit();
  const p = state.preview;
  const data = await api(base() + `/config/${p.config}`, {
    method: "PUT",
    body: JSON.stringify({ content: p.content, revision: p.revision }),
  });
  if (state.preview !== p) return;
  Object.assign(p, data, { dirty: false });
  renderPreview(p.source, false);
  toast("已保存并创建历史快照");
});
listen("historyButton", "click", async () => {
  const p = state.preview;
  const url = base();
  const rows = await api(url + `/config/${p.config}/history`);
  if (state.preview !== p) return;
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
              url + `/config/${p.config}/history/${el.dataset.restore}/restore`,
              {
                method: "POST",
                body: JSON.stringify({ revision: p.revision }),
              },
            );
            if (state.preview !== p) return;
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
    .forEach((el) => (el.onclick = () => launchTemplate(el.dataset.kind)));
  await loadProjects();
  const saved = localStorage.getItem("transmux-v2-workspace");
  if (state.projects.length)
    await selectProject(
      state.projects.some((p) => p.id === saved) ? saved : state.projects[0].id,
    );
}
start().catch((e) => reportError(e));

// Keep the library entries beneath the Agent; show their content only in a dialog.
libraryElement.querySelectorAll('[data-library]').forEach(button => button.onclick = () => {
  const documents = button.dataset.library === 'documents';
  $('libraryTitle').textContent = `${project()?.name || 'Agent'} · ${documents ? '文档库' : '翻译资源库'}`;
  $('tree').hidden = !documents;
  $('resources').hidden = documents;
  $('libraryDialog').showModal();
});
let styleRuleReview = null;
async function openStyleRules(rid) {
  const generation = state.generation;
  const review = await api(base() + `/reviews/${rid}`);
  if (generation !== state.generation) return;
  const entries = review.entries ? JSON.parse(review.entries) : [{id: 'legacy', text: review.content, evidence: []}];
  styleRuleReview = {rid, generation, structured: !!review.entries};
  $('styleRulesList').innerHTML = entries.map(row => `<article class="style-rule"><label><input type="checkbox" data-style-rule="${esc(row.id)}" checked><strong>${esc(row.text)}</strong></label>${row.evidence.map(e => `<div class="style-evidence"><span>${esc(e.source)} · 段落 ${esc(e.paragraph)}</span><blockquote>${esc(e.quote)}</blockquote></div>`).join('') || '<p class="hint">历史风格未记录逐条出处，请核对后确认。</p>'}</article>`).join('');
  $('selectAllStyleRules').checked = true;
  $('selectAllStyleRules').indeterminate = false;
  $('approveStyleRules').disabled = false;
  $('styleRulesList').onchange = updateRuleSelection;
  $('styleRulesDialog').showModal();
}
function updateRuleSelection() {
  const rows = [...$('styleRulesList').querySelectorAll('[data-style-rule]')];
  const count = rows.filter(el => el.checked).length;
  $('selectAllStyleRules').checked = count === rows.length;
  $('selectAllStyleRules').indeterminate = count > 0 && count < rows.length;
  $('approveStyleRules').disabled = !count;
}
$('selectAllStyleRules').onchange = () => {
  $('styleRulesList').querySelectorAll('[data-style-rule]').forEach(el => el.checked = $('selectAllStyleRules').checked);
  updateRuleSelection();
};
$('approveStyleRules').onclick = async () => {
  const current = styleRuleReview;
  if (!current || current.generation !== state.generation) return;
  const ids = [...$('styleRulesList').querySelectorAll('[data-style-rule]:checked')].map(el => el.dataset.styleRule);
  if (!ids.length) return;
  $('approveStyleRules').disabled = true;
  try {
    await api(base() + `/reviews/${current.rid}/approve`, {method: 'POST', body: JSON.stringify(current.structured ? {selected_rule_ids: ids} : {})});
    if (current.generation !== state.generation) return;
    $('styleRulesDialog').close();
    await refresh();
    toast('已生成审核通过的翻译风格 Markdown');
  } catch (e) { reportError(e); $('approveStyleRules').disabled = false; }
};
