const $ = (id) => document.getElementById(id);
const state = {project: null, projects: [], files: [], artifacts: [], jobs: [], filter: 'output', stream: null, cursor: 0, editors: {}, refreshTimer: null};
const languageNames = {en:'English', 'zh-CN':'简体中文'};
const names = {style:'风格与术语更新', rag:'构建 RAG', recall:'召回测试', translate:'文档翻译', revise:'段落修改', chat:'对话微调', layout:'排版与导出', terminology_review:'旧术语重新筛选'};
const statuses = {queued:'排队中', running:'执行中', succeeded:'已完成', failed:'失败', needs_attention:'需处理', cancelled:'已停止', interrupted:'已中断'};
const escapeHTML = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const path = (suffix = '') => `/api/projects/${state.project.id}${suffix}`;
let toastTimer;
function toast(message, error = false) {
  $('toast').textContent = message; $('toast').className = error ? 'error' : ''; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, error ? 8000 : 3500);
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求参数无效，请检查输入');
  return data;
}
const jsonOptions = (method, data) => ({method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
function bind(id, action) {
  $(id).addEventListener('click', async () => {
    $(id).disabled = true;
    try { await action(); } catch (error) { toast(error.message, true); }
    finally { $(id).disabled = false; }
  });
}
function drawProjects() {
  $('project-list').innerHTML = state.projects.map(p => `<button class="project-button ${state.project?.id === p.id ? 'active' : ''}" data-project="${p.id}" title="${escapeHTML(p.name)}">${escapeHTML(p.name)}</button>`).join('');
}
async function loadConfig(name, force = false) {
  if (name === 'requirements' && state.supportsRequirements === false) return;
  const pid = state.project.id;
  const editor = state.editors[name];
  if (!force && (editor?.dirty || editor?.saving)) return;
  const data = await api(path(`/config/${name}`));
  if (state.project?.id !== pid || (!force && (state.editors[name]?.dirty || state.editors[name]?.saving))) return;
  state.editors[name] = {revision: data.revision, dirty:false, saving:null, timer:null};
  const changed = $(name + '-editor').value !== data.content;
  if (changed) $(name + '-editor').value = data.content;
  if (!['style', 'requirements'].includes(name) && (changed || force)) renderTerminology(name);
  editorStatus(name, '已同步');
}
function editorStatus(name, text, type = '') { $(name + '-status').textContent = text; $(name + '-status').className = `save-state ${type}`; }
async function saveConfig(name) {
  const editor = state.editors[name];
  if (!editor) return;
  clearTimeout(editor.timer);
  if (editor.saving) await editor.saving;
  if (!editor.dirty) return;
  const content = $(name + '-editor').value;
  editorStatus(name, '保存中…');
  editor.saving = api(path(`/config/${name}`), jsonOptions('PUT', {content, revision:editor.revision}));
  try {
    const data = await editor.saving;
    editor.revision = data.revision;
    editor.dirty = $(name + '-editor').value !== content;
    if (!editor.dirty && data.content !== content) { $(name + '-editor').value = data.content; if (!['style', 'requirements'].includes(name)) renderTerminology(name); }
    editorStatus(name, editor.dirty ? '待保存' : '已同步', editor.dirty ? 'dirty' : '');
    if (name === 'style') await loadConfig('requirements');
  } catch (error) {
    editorStatus(name, '保存冲突 / 失败', 'error');
    throw error;
  } finally { editor.saving = null; }
  if (editor.dirty) await saveConfig(name);
}
async function flushEditors() { await saveConfig('style'); await saveConfig('requirements'); await saveConfig('terms'); await saveConfig('mappings'); await saveConfig('people'); }
for (const name of ['style','requirements','terms','mappings','people']) {
  $(name + '-editor').addEventListener('input', () => {
    const editor = state.editors[name];
    if (!editor) return;
    if (!['style', 'requirements'].includes(name)) { try { renderTerminology(name); } catch { /* Keep invalid raw text for correction. */ } }
    editor.dirty = true; editorStatus(name, '待保存', 'dirty'); clearTimeout(editor.timer);
    editor.timer = setTimeout(() => saveConfig(name).catch(e => toast(e.message, true)), 700);
  });
}
document.querySelectorAll('[data-reload]').forEach(button => button.addEventListener('click', async () => {
  const name = button.dataset.reload;
  if (state.editors[name]?.dirty && !confirm('重新载入将替换编辑框中尚未保存的内容。继续吗？')) return;
  try {
    clearTimeout(state.editors[name]?.timer);
    if (state.editors[name]?.saving) await state.editors[name].saving.catch(() => {});
    await loadConfig(name, true);
  } catch (e) { toast(e.message, true); }
}));
async function selectProject(pid) {
  await flushEditors();
  if (typeof closeComparison === 'function') closeComparison();
  if (typeof closeLayout === 'function') closeLayout();
  if (typeof closeTermTools === 'function') closeTermTools();
  state.stream?.close(); state.stream = null;
  state.project = state.projects.find(p => p.id === pid); state.cursor = 0; state.editors = {}; state.jobs = [];
  state.filter = 'output';
  document.querySelectorAll('[data-file-filter]').forEach(button => button.classList.toggle('active', button.dataset.fileFilter === 'output'));
  $('file-list').innerHTML = '<p class="empty">正在加载译文…</p>';
  localStorage.setItem('transmux.project', pid);
  $('onboarding').hidden = true; $('workspace').hidden = false;
  $('target-language').value = languageNames[state.project.target_language] || 'English';
  $('use-rag').checked = Boolean(state.project.use_rag);
  $('project-title').textContent = state.project.name; $('breadcrumb-name').textContent = state.project.name;
  updateModelBadge();
  $('model-menu').open = false;
  $('agent-name').textContent = state.project.agent === 'codex' ? 'Codex CLI' : 'CodeBuddy';
  $('messages').innerHTML = '<div class="chat-empty"><span>✳</span><h3>你的翻译搭档，已就位</h3><p>任务进度会显示在这里。<br>也可以直接提出修改要求。</p></div>';
  $('recall-results').innerHTML = ''; $('chat-input').value = '';
  $('corpus-summary').classList.remove('is-empty');
  $('corpus-summary-title').textContent = '正在加载参考语料…';
  $('corpus-guidance').textContent = '';
  $('corpus-list').innerHTML = '';
  renderStyleUpdate(false);
  drawProjects();
  $('project-model').value = state.project.model || '';
  $('model-status').textContent = '运行中的任务保持原模型，新任务开始时生效。';
  loadModels(state.project.agent, 'project-model-options').catch(e => toast(e.message, true));
  await Promise.all([loadConfig('style', true), loadConfig('requirements', true), loadConfig('terms', true), loadConfig('mappings', true), loadConfig('people', true), refreshProject()]);
  if (state.project?.id !== pid) return;
  state.stream = new EventSource(path('/stream'));
  state.stream.onopen = () => $('service-status').textContent = '服务已连接';
  state.stream.onerror = () => $('service-status').textContent = '连接中断，正在重连';
  state.stream.onmessage = (message) => {
    if (state.project?.id !== pid) return;
    const event = JSON.parse(message.data);
    if (event.id <= state.cursor) return;
    state.cursor = event.id;
    if (event.kind === 'phase') {
      const job = state.jobs.find(j => j.id === event.job);
      if (job && ['running','queued'].includes(job.state)) { job.state = 'running'; job.progress = event.text; renderJobs(); }
      else scheduleRefresh();
    }
    if (statuses[event.kind]) {
      const job = state.jobs.find(j => j.id === event.job);
      if (job) { job.state = event.kind; job.result = event.text; renderJobs(); }
    }
    appendEvent(event);
    if (event.kind === 'user' || statuses[event.kind]) scheduleRefresh();
  };
}
function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => refreshProject().catch(e => toast(e.message, true)), 200);
}
function renderStyleUpdate(pending = state.stylePending) {
  state.stylePending = Boolean(pending);
  const active = state.jobs.find(job => job.kind === 'style' && job.state === 'running')
    || state.jobs.find(job => job.kind === 'style' && job.state === 'queued');
  const highlight = state.stylePending && !active;
  const button = $('extract-style');
  button.classList.toggle('primary', highlight);
  button.classList.toggle('secondary', !highlight);
  button.textContent = '✧ 更新风格与术语' + (active ? ` · ${active.state === 'queued' ? '排队中' : '更新中'}` : highlight ? ' · 待更新' : '');
  button.title = active ? '更新任务已提交，可在 Agent 区域查看进度' : highlight ? '参考语料有变动，点击更新风格、术语和参考索引' : '重新从参考语料提取风格与术语';
}
async function refreshProject() {
  if (!state.project) return;
  const pid = state.project.id;
  const [files, jobs, artifacts, index] = await Promise.all([api(path('/files')), api(path('/jobs')), api(path('/artifacts')), api(path('/rag-status')).catch(() => ({state:'pending', message:'参考索引随风格更新自动同步'}))]);
  if (state.project?.id !== pid) return;
  state.files = files; state.jobs = jobs; state.artifacts = artifacts;
  $('rag-status').textContent = index.state === 'ready' ? `参考索引已同步 · ${index.included} 段目标语言语料 · 排除 ${index.excluded} 段其他语言或不确定内容` : index.message;
  $('build-rag').hidden = index.state !== 'failed';
  renderStyleUpdate(Boolean(index.style_pending));
  renderFiles(); renderJobs();
  const old = $('source-file').value;
  const sources = files.filter(f => f.kind === 'source');
  $('source-file').innerHTML = sources.length ? sources.map(f => `<option value="${f.id}">${escapeHTML(f.name)}</option>`).join('') : '<option value="">请先上传文档</option>';
  if (sources.some(f => f.id === old)) $('source-file').value = old;
  const corpus = files.filter(f => f.kind === 'corpus');
  const target = languageNames[state.project.target_language] || 'English';
  $('corpus-summary').classList.toggle('is-empty', !corpus.length);
  $('corpus-summary-title').textContent = corpus.length ? `已上传 ${corpus.length} 份参考语料` : '尚未上传参考语料';
  $('corpus-guidance').textContent = corpus.length
    ? `上传后点击“更新风格与术语”，学习 ${target} 的表达方式并自动同步参考索引。可继续追加或删除指定文档。`
    : `建议上传 ${target} 的优质文档，用于提取风格、术语及翻译时的相似段落参考。暂不上传也可使用现有风格与术语直接翻译。`;
  const busy = jobs.some(job => ['queued','running'].includes(job.state));
  $('delete-workspace').disabled = busy;
  $('delete-workspace').title = busy ? '请等待任务完成或停止任务后再删除' : '';
  $('corpus-list').innerHTML = corpus.map(file => `<div class="file-row"><span class="file-icon">${escapeHTML(file.name.split('.').pop().toUpperCase())}</span><div class="file-info"><strong title="${escapeHTML(file.name)}">${escapeHTML(file.name)}</strong><small>${new Date(file.created * 1000).toLocaleString('zh-CN')}</small></div><a href="${path('/files/' + file.id + '/download')}" download title="下载 ${escapeHTML(file.name)}">↓</a><button class="text-button danger-text" data-delete-corpus="${file.id}" ${busy ? 'disabled title="任务结束后可删除"' : ''} aria-label="删除 ${escapeHTML(file.name)}">删除</button></div>`).join('');
  const recall = jobs.find(j => j.kind === 'recall' && j.state === 'succeeded');
  if (recall) renderRecall(JSON.parse(recall.result).matches.filter(m => corpus.some(f => f.id === m.file_id)));
  else $('recall-results').innerHTML = '';
  await Promise.all([loadConfig('style'), loadConfig('requirements'), loadConfig('terms'), loadConfig('mappings'), loadConfig('people')]);
}
function renderFiles() {
  $('file-count').textContent = `${state.files.length} FILES`;
  if (state.filter === 'artifacts') {
    $('file-list').innerHTML = state.artifacts.length ? state.artifacts.map(f => `<div class="file-row"><span class="file-icon">LOG</span><div class="file-info"><strong>${escapeHTML(f.name)}</strong><small>${escapeHTML(f.path.split('/')[1].slice(0,8))}</small></div><a href="${path('/artifact/' + f.path.split('/').map(encodeURIComponent).join('/'))}" title="下载 ${escapeHTML(f.name)}" download>↓</a></div>`).join('') : '<p class="empty">任务的配置快照、召回依据和审校记录将显示在这里。</p>';
    return;
  }
  const files = state.files.filter(f => state.filter === 'all' || f.kind === state.filter || state.filter === 'output' && f.kind === 'edited');
  $('file-list').innerHTML = files.length ? files.map(f => `<div class="file-row"><span class="file-icon">${escapeHTML(f.name.split('.').pop().toUpperCase().slice(0,4))}</span><div class="file-info"><strong>${escapeHTML(f.name)}</strong><small>${{corpus:'参考语料', source:'待翻译原文', manuscript:'独立排版文稿', output:f.parent_file ? '段落修订版 · 审校通过' : '审校通过的译文', edited:'对话修改版 · 未重新自动审校'}[f.kind]} · ${new Date(f.created * 1000).toLocaleDateString('zh-CN')}</small></div>${f.kind === 'output' ? '<span class="file-badge">审校通过</span>' : ''}${f.can_layout ? `<button class="text-button layout-file-entry" data-layout="${f.id}">排版与导出</button>` : ''}${f.can_compare ? `<button class="text-button compare-entry" data-compare="${f.id}">对照</button>` : ''}<a href="${path('/files/' + f.id + '/download')}" download title="下载 ${escapeHTML(f.name)}">↓</a></div>`).join('') : state.filter === 'output' ? '<p class="empty">还没有译文。<br>完成翻译后，可在这里对照、排版和下载。</p>' : '<p class="empty">此分类暂无文件。</p>';
}
function renderJobs() {
  renderStyleUpdate();
  const running = state.jobs.find(j => j.state === 'running'); const queued = state.jobs.filter(j => j.state === 'queued').length;
  $('queue-label').textContent = running ? `${names[running.kind]}中${queued ? ` · ${queued} 项排队` : ''}` : queued ? `${queued} 项排队中` : '准备就绪';
  renderActiveStatus();
  if (typeof refreshComparisonState === 'function') refreshComparisonState();
  if (typeof refreshLayoutState === 'function') refreshLayoutState();
  $('job-list').innerHTML = state.jobs.slice(0,8).map(j => `<div class="job-row"><span>${names[j.kind]}</span><span class="job-state ${j.state}">${statuses[j.state]}</span>${['queued','running'].includes(j.state) ? `<button data-cancel="${j.id}">停止</button>` : ''}</div>`).join('');
}
function renderRecall(matches) {
  $('recall-results').innerHTML = matches.map(m => `<div class="recall-result"><small>${escapeHTML(m.source)} · 段落 ${m.paragraph} · 相似度 ${m.score.toFixed(3)}</small>${escapeHTML(m.text)}</div>`).join('');
}
function appendEvent(event) {
  let text = event.kind === 'phase' ? (() => { const p = JSON.parse(event.text); return `${p.title} · ${p.detail}`; })() : event.text; let detail = false;
  if (event.kind === 'agent') {
    try {
      const e = JSON.parse(text);
      if (e.type === 'item.completed' && e.item?.type === 'agent_message') text = e.item.text;
      else if (e.type === 'assistant') {
        text = (e.message?.content || []).filter(c => c.type === 'text').map(c => c.text).join('\n');
        if (!text) return;
      } else if (e.type === 'item.started' && e.item?.type === 'command_execution') text = `执行：${e.item.command}`;
      else if (e.type === 'error' || e.type === 'turn.failed') { text = JSON.stringify(e.error || e); }
      else return;
      if (text.trim().startsWith('{') || text.length > 1800) detail = true;
    } catch { detail = true; }
  }
  if (event.kind === 'succeeded' || event.kind === 'needs_attention') {
    try {
      const value = JSON.parse(text);
      if (value.export_id) text = value.message;
      else if (value.matches) text = `召回完成，找到 ${value.matches.length} 个参考段落。`;
      else if (value.file_id) text = `译文已通过审校：${value.name}\n可在项目文件区下载 DOCX。`;
      else if (value.chunks !== undefined) text = `RAG 构建完成：${value.files} 份语料，${value.chunks} 个语义片段。`;
    } catch { /* Plain agent message. */ }
  }
  const messages = $('messages');
  const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
  messages.querySelector('.chat-empty')?.remove();
  const node = document.createElement('div'); node.className = `message ${event.kind}`;
  const label = event.kind === 'user' ? '你' : statuses[event.kind] || (event.kind === 'progress' ? '执行进度' : 'Agent');
  node.innerHTML = `<div class="message-label">${label} · ${new Date(event.created * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'})}</div>${detail ? `<details><summary>查看 Agent 输出</summary><pre>${escapeHTML(text)}</pre></details>` : `<div class="message-body">${escapeHTML(text)}</div>`}`;
  if (event.kind === 'succeeded') {
    try {
      const result = JSON.parse(event.text);
      if (result.new_mappings !== undefined) {
        const button = document.createElement('button'); button.className = 'text-button';
        button.textContent = `新增 ${result.new_mappings} 条翻译对照 · 查看 →`;
        button.addEventListener('click', () => { showTermTab('mappings'); $('terminology-card').scrollIntoView({behavior:'smooth',block:'center'}); });
        node.appendChild(button);
      }
    } catch { /* Plain result. */ }
  }
  messages.appendChild(node);
  while (messages.children.length > 300) messages.firstChild.remove();
  if (nearBottom || event.kind === 'user') messages.scrollTop = messages.scrollHeight;
}
async function submitJob(kind, extra = {}) {
  if (state.pendingUpgrade) throw new Error('服务将在现有任务完成后自动升级，请稍后提交新任务');
  await flushEditors();
  const job = await api(path('/jobs'), jsonOptions('POST', {kind, ...extra}));
  toast('任务已进入本项目队列'); await refreshProject(); return job;
}
async function uploadFiles(input, kind) {
  const pid = state.project.id; const files = Array.from(input.files); input.disabled = true;
  try {
    for (const file of files) {
      const data = new FormData(); data.append('file', file);
      toast(`正在解析 ${file.name}…`);
      await api(`/api/projects/${pid}/files?kind=${kind}`, {method:'POST', body:data});
    }
    toast(`已上传 ${files.length} 份文件${kind === 'corpus' ? '，更新风格时会自动同步参考索引' : ''}`);
    await refreshProject();
  } catch (e) { toast(e.message, true); }
  finally { input.disabled = false; input.value = ''; }
}
$('source-upload').addEventListener('change', e => uploadFiles(e.target, 'source'));
$('corpus-upload').addEventListener('change', e => uploadFiles(e.target, 'corpus'));
$('upload-corpus-button').addEventListener('click', () => $('corpus-upload').click());
bind('translate', () => {
  if (!$('source-file').value) throw new Error('请先上传待翻译文档');
  return submitJob('translate', {file_id:$('source-file').value, use_rag:$('use-rag').checked, max_review_rounds:Number($('review-rounds').value)});
});
bind('extract-style', () => submitJob('style'));
bind('build-rag', () => submitJob('rag'));
bind('test-recall', () => submitJob('recall', {message:$('recall-query').value}));
$('chat-form').addEventListener('submit', async e => {
  e.preventDefault(); const message = $('chat-input').value.trim(); if (!message) return;
  const button = $('chat-form').querySelector('button'); button.disabled = true;
  try { await submitJob('chat', {message}); $('chat-input').value = ''; }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
$('chat-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!$('chat-form').querySelector('button').disabled) $('chat-form').requestSubmit(); }
});
$('job-list').addEventListener('click', async e => {
  const button = e.target.closest('[data-cancel]'); if (!button) return;
  try { await api(path(`/jobs/${button.dataset.cancel}/cancel`), {method:'POST'}); toast('停止请求已发送，正在清理当前任务'); await refreshProject(); }
  catch (error) { toast(error.message, true); }
});
document.querySelectorAll('[data-file-filter]').forEach(button => button.addEventListener('click', () => {
  state.filter = button.dataset.fileFilter;
  document.querySelectorAll('[data-file-filter]').forEach(b => b.classList.toggle('active', b === button)); renderFiles();
}));
document.querySelectorAll('[data-scroll]').forEach(button => button.addEventListener('click', () => {
  $(button.dataset.scroll).scrollIntoView({behavior:'smooth',block:'start'});
  document.querySelectorAll('[data-scroll]').forEach(item => {
    item.classList.toggle('selected', item === button);
    if (item === button) item.setAttribute('aria-current', 'location');
    else item.removeAttribute('aria-current');
  });
}));
$('project-list').addEventListener('click', e => {
  const button = e.target.closest('[data-project]'); if (button && button.dataset.project !== state.project?.id) selectProject(button.dataset.project).catch(e => toast(e.message, true));
});
for (const id of ['new-project','start-project']) $(id).addEventListener('click', () => { $('create-error').textContent = ''; $('project-dialog').showModal(); $('project-name').focus(); $('create-model').value = ''; loadCreateModels(); });
$('close-dialog').addEventListener('click', () => $('project-dialog').close());
$('project-form').addEventListener('submit', async e => {
  e.preventDefault(); const button = $('project-form').querySelector('button[type="submit"]'); button.disabled = true;
  try {
    await flushEditors();
    const project = await api('/api/projects', jsonOptions('POST', {name:$('project-name').value, agent:new FormData(e.target).get('agent'), target_language:$('create-language').value, model:$('create-model').value.trim() || null}));
    state.projects.push(project); await selectProject(project.id); $('project-dialog').close(); $('project-name').value = '';
  } catch (error) { $('create-error').textContent = error.message; }
  finally { button.disabled = false; }
});
window.addEventListener('beforeunload', e => { if (Object.values(state.editors).some(v => v.dirty || v.saving)) { e.preventDefault(); e.returnValue = ''; } });
async function start() {
  try {
    const [health, projects] = await Promise.all([api('/api/health'), api('/api/projects')]);
    state.supportsRequirements = health.workflow_version >= 14;
    document.querySelector('.style-requirements').hidden = !state.supportsRequirements;
    state.pendingUpgrade = ![13, 14, 15].includes(health.workflow_version);
    $('deployment-banner').hidden = !state.pendingUpgrade;
    for (const id of ['translate','extract-style','test-recall','use-rag','new-project','start-project','build-rag','retry-task']) $(id).disabled = state.pendingUpgrade;
    $('chat-form').querySelector('button').disabled = state.pendingUpgrade;
    $('service-status').textContent = '服务已连接';
    for (const agent of health.agents) {
      $(agent.id + '-availability').textContent = agent.available ? '已安装' : '未安装';
      document.querySelector(`input[name="agent"][value="${agent.id}"]`).disabled = !agent.available;
    }
    const first = document.querySelector('input[name="agent"]:not(:disabled)'); if (first) first.checked = true;
    state.projects = projects; drawProjects();
    if (projects.length) await selectProject(projects.find(p => p.id === localStorage.getItem('transmux.project'))?.id || projects[0].id);
    else $('onboarding').hidden = false;
  } catch (error) { $('service-status').textContent = '连接失败'; toast(error.message, true); }
}
setInterval(() => { if (state.project && !document.hidden) refreshProject().catch(() => $('service-status').textContent = '服务连接异常'); }, 5000);
setInterval(async () => {
  if (!state.pendingUpgrade) return;
  try {
    const health = await api('/api/health');
    if ([13, 14, 15].includes(health.workflow_version) && !Object.values(state.editors).some(e => e.dirty || e.saving)) location.reload();
  } catch { /* The process may be restarting. */ }
}, 5000);
start();

async function loadModels(agent, target) {
  const pid = state.project?.id;
  const result = await api(`/api/agents/${agent}/models`);
  if (target === 'project-model-options' && (state.project?.agent !== agent || state.project?.id !== pid)) return;
  if (target === 'create-model-options' && document.querySelector('input[name="agent"]:checked')?.value !== agent) return;
  const input = $(target.replace('-options', ''));
  $(target).innerHTML = '<option value="">沿用 Agent 会话 / 默认设置</option>' + result.models.map(m => `<option value="${escapeHTML(m)}">${escapeHTML(m)}</option>`).join('') + '<option value="__custom__">自定义模型 ID…</option>';
  $(target).value = !input.value || result.models.includes(input.value) ? input.value : '__custom__';
  input.hidden = $(target).value !== '__custom__';
}
function loadCreateModels() {
  const agent = document.querySelector('input[name="agent"]:checked')?.value;
  if (agent) loadModels(agent, 'create-model-options').catch(e => toast(e.message, true));
}
document.querySelectorAll('input[name="agent"]').forEach(input => input.addEventListener('change', () => {
  $('create-model').value = ''; loadCreateModels();
}));
bind('save-model', async () => {
  const pid = state.project.id;
  const project = await api(`/api/projects/${pid}`, jsonOptions('PATCH', {model:$('project-model').value.trim() || null}));
  state.projects = state.projects.map(p => p.id === pid ? project : p);
  if (state.project.id === pid) {
    state.project = project;
    updateModelBadge();
    $('model-menu').open = false;
    $('project-model').value = project.model || '';
    $('model-status').textContent = `已保存：${project.model || '沿用 Agent 设置'} · 新任务开始时生效`;
  }
  toast('项目模型已保存');
});

let historyView = null;
const historySources = {initial:'初始版本', existing:'升级时保留', manual:'手动编辑', extraction:'语料提取', agent:'Agent 修改', external:'工作区变更', restore:'历史恢复', translation:'翻译积累'};
async function loadHistory(more = false) {
  const view = historyView;
  const before = more && view.rows.length ? view.rows[view.rows.length - 1].id : 0;
  const rows = await api(`/api/projects/${view.pid}/config/${view.name}/history?before=${before}`);
  if (historyView !== view) return;
  view.rows = more ? view.rows.concat(rows) : rows;
  $('history-select').innerHTML = view.rows.map(row => `<option value="${row.id}">#${row.id} · ${escapeHTML(new Date(row.created * 1000).toLocaleString('zh-CN'))} · ${historySources[row.source] || escapeHTML(row.source)}</option>`).join('');
  $('more-history').hidden = rows.length < 100;
  previewHistory();
}
function previewHistory() {
  const row = historyView?.rows.find(r => String(r.id) === $('history-select').value);
  $('history-preview').textContent = row?.content || '';
  $('restore-history').disabled = !row;
}
document.querySelectorAll('[data-history]').forEach(button => button.addEventListener('click', async () => {
  try {
    await saveConfig(button.dataset.history);
    historyView = {pid:state.project.id, name:button.dataset.history, rows:[], revision:state.editors[button.dataset.history].revision};
    $('history-title').textContent = ({style:'翻译风格',requirements:'用户风格要求',terms:'目标语言术语',mappings:'翻译对照',people:'人名'}[historyView.name]) + ' · 历史快照';
    $('history-error').textContent = '';
    await loadHistory();
    $('history-dialog').showModal();
  } catch (e) { toast(e.message, true); }
}));
$('history-select').addEventListener('change', previewHistory);
$('close-history').addEventListener('click', () => $('history-dialog').close());
bind('more-history', () => loadHistory(true));
bind('restore-history', async () => {
  const view = historyView;
  const row = view.rows.find(r => String(r.id) === $('history-select').value);
  if (!row) return;
  try {
    const restored = await api(`/api/projects/${view.pid}/config/${view.name}/history/${row.id}/restore`, jsonOptions('POST', {revision:view.revision}));
    view.revision = restored.revision;
    if (state.project.id === view.pid) await loadConfig(view.name, true);
    await loadHistory();
    toast('已恢复历史版本，原版本仍可在历史中查看');
  } catch (e) { $('history-error').textContent = e.message; }
});

for (const prefix of ['project', 'create']) {
  $(prefix + '-model-options').addEventListener('change', () => {
    const select = $(prefix + '-model-options');
    const input = $(prefix + '-model');
    input.hidden = select.value !== '__custom__';
    if (select.value !== '__custom__') input.value = select.value;
    else { input.value = ''; input.focus(); }
    if (prefix === 'project') $('model-status').textContent = '点击“应用模型”保存，从下一项任务开始生效。';
  });
}

function updateModelBadge() {
  const project = state.project;
  if (!project) return;
  const agent = project.agent === 'codex' ? 'Codex CLI' : 'CodeBuddy';
  $('agent-pill').textContent = `${agent} · 已绑定 · ${project.model || '沿用 Agent 设置'} ▾`;
  $('agent-pill').title = `${agent} · ${project.model || '沿用 Agent 设置'} — 点击切换模型`;
  $('agent-pill').removeAttribute('aria-disabled');
}
$('agent-pill').addEventListener('click', e => { if (!state.project) e.preventDefault(); });
document.addEventListener('click', e => {
  if (!$('model-menu').contains(e.target)) $('model-menu').open = false;
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('model-menu').open) { $('model-menu').open = false; $('agent-pill').focus(); }
});

function renderActiveStatus() {
  const job = state.jobs.find(j => j.state === 'running') || state.jobs.find(j => j.state === 'queued') || state.jobs[0];
  const running = job?.state === 'running';
  const progress = job?.progress ? JSON.parse(job.progress) : {};
  $('active-status').dataset.state = job?.state || 'idle';
  $('active-title').textContent = running ? progress.title || '准备中' : job ? `${names[job.kind]} · ${statuses[job.state]}` : '准备就绪';
  $('active-detail').textContent = running ? progress.detail || '正在启动 Agent' : job?.state === 'queued' ? '已进入本项目队列，等待本项目当前任务完成。' : ['failed','needs_attention','interrupted'].includes(job?.state) ? job.result : job?.state === 'succeeded' ? '任务已完成，可查看下方记录与项目文件。' : '提交任务后，执行阶段将实时显示在这里。';
  if (job?.kind === 'layout' && !running && job.result) {
    try { $('active-detail').textContent = JSON.parse(job.result).message || job.result; }
    catch { $('active-detail').textContent = job.result; }
  }
  $('active-progress').hidden = !running || !progress.total;
  $('active-progress').max = progress.total || 1;
  $('active-progress').value = progress.completed || 0;
  $('active-count').textContent = running && progress.total ? `已通过审校 ${progress.completed || 0} / ${progress.total} 段` : '';
  $('active-age').dataset.since = running ? progress.updated || job.created : '';
  $('active-age').dataset.stage = running ? progress.stage || '' : '';
  renderStageWarning();
  $('retry-task').hidden = !['failed','needs_attention','interrupted','cancelled'].includes(job?.state);
  $('retry-task').dataset.job = job?.id || '';
}
function renderStageWarning() {
  const since = Number($('active-age').dataset.since);
  const slow = since && Date.now()/1000 - since >= 60;
  const rag = ['recalling', 'indexing'].includes($('active-age').dataset.stage);
  $('active-warning').hidden = !slow;
  $('active-warning').textContent = rag ? '当前阶段已超过 60 秒，可能在等待向量模型或向量服务。尚未返回结果；可在下方停止任务后重试，或关闭 RAG。' : '当前阶段已超过 60 秒，尚未收到阶段完成结果。请查看下方执行记录；必要时可停止任务。';
}
setInterval(() => {
  renderStageWarning();
  const since = Number($('active-age').dataset.since);
  $('active-age').textContent = since ? `${Math.max(0, Math.floor(Date.now()/1000 - since))}s` : '';
}, 1000);
$('use-rag').addEventListener('change', async () => {
  const input = $('use-rag'); const value = input.checked; const pid = state.project.id; input.disabled = true;
  try {
    const updated = await api(`/api/projects/${pid}/rag-preference`, jsonOptions('PATCH', {use_rag:value}));
    state.projects = state.projects.map(p => p.id === pid ? updated : p);
    if (state.project.id === pid) state.project = updated;
  } catch (e) { if (state.project.id === pid) input.checked = !value; toast(e.message, true); }
  finally { input.disabled = false; }
});
bind('retry-task', () => {
  const job = state.jobs.find(j => j.id === $('retry-task').dataset.job);
  if (!job) return;
  const payload = JSON.parse(job.payload);
  const input = {};
  for (const key of ['file_id','paragraph','message','top_k','max_review_rounds','template','source_sha256','roles']) if (payload[key] != null) input[key] = payload[key];
  input.use_rag = $('use-rag').checked;
  return submitJob(job.kind, input);
});

const termFields = {terms:[['term','首选术语'],['usage','使用规范'],['scope','适用语境']],mappings:[['original','原文术语'],['translation','目标译法'],['context','适用语境']],people:[['original','原文人名'],['translation','标准写法'],['context','身份 / 适用语境']]};
const termDetails = {terms:[['meaning','含义'],['reason','收录理由'],['evidence','原文证据']],mappings:[['reason','收录理由'],['evidence','原文证据']],people:[['aliases','已确认别名（分号分隔）'],['reason','确认依据'],['evidence','原文证据']]};
const termOrigins = {manual:'用户指定',extraction:'语料提取',translation:'翻译积累'};
const termStatuses = {active:'已生效',pending:'待确认',inactive:'已停用'};
let currentTermTab = 'terms';
function termIdentity(name, row) {
  const normalize = text => (text || '').toLowerCase().trim().replace(/\s+/g, ' ');
  let identity = name === 'terms' ? normalize(row.term) + (row.scope?.trim() ? '\u241f' + normalize(row.scope) : '') : normalize(row.original) + '\u241f' + normalize(row.context);
  return identity + (row.variant ? '\u241e' + row.variant : '');
}
function showTermTab(name) {
  currentTermTab = name;
  for (const kind of ['terms','mappings','people']) {
    $('panel-' + kind).hidden = name !== kind;
    $('tab-' + kind).setAttribute('aria-selected', String(name === kind));
    $('tab-' + kind).tabIndex = name === kind ? 0 : -1;
  }
  if ($(name + '-editor').value) renderTerminology(name);
}
function renderTerminology(name) {
  const doc = JSON.parse($(name + '-editor').value);
  if (!Array.isArray(doc.rows)) return;
  const query = $('term-search').value.trim().toLowerCase();
  const filter = $('term-filter').value;
  const rows = doc.rows.map((row, i) => ({row,i})).filter(({row}) => (filter === 'all' || (row.status || 'active') === filter) && (!query || Object.values(row).join(' ').toLowerCase().includes(query)));
  const fieldHTML = (row, i, field, label) => `<textarea rows="2" data-term-kind="${name}" data-row="${i}" data-field="${field}" aria-label="第 ${i+1} 条${label}">${escapeHTML(row[field] || '')}</textarea>`;
  $(name + '-table').innerHTML = rows.length ? `<table class="term-table"><thead><tr>${termFields[name].map(([,label])=>`<th>${label}</th>`).join('')}</tr></thead><tbody>${rows.map(({row,i})=>`<tr>${termFields[name].map(([field,label])=>`<td>${fieldHTML(row,i,field,label)}</td>`).join('')}</tr><tr class="term-meta"><td colspan="3"><div><span class="term-state ${row.status || 'active'}">${termStatuses[row.status || 'active']}</span><span class="term-origin ${row.origin === 'manual' ? 'manual' : ''}">${termOrigins[row.origin] || '未知'}</span><small>${escapeHTML(row.source)}</small>${(row.status || 'active') !== 'active' ? `<button class="text-button" data-term-resolve="activate" data-term-kind="${name}" data-row="${i}">确认生效</button>` : ''}${row.status !== 'inactive' ? `<button class="text-button" data-term-resolve="deactivate" data-term-kind="${name}" data-row="${i}">停用</button>` : ''}<button class="text-button" data-delete-term="${name}" data-row="${i}" aria-label="删除第 ${i+1} 条">删除</button></div><details class="term-evidence"><summary>解释与原文证据${row.conflict ? ' · 与现有条目冲突' : ''}</summary>${row.conflict ? `<p>已有写法：${escapeHTML(Object.entries(doc.rows.find(r => termIdentity(name,r) === row.conflict) || {}).filter(([k]) => termFields[name].some(([f]) => f === k)).map(([,v])=>v).join(' · '))}</p>` : ''}${termDetails[name].map(([field,label])=>`<label class="field">${label}${fieldHTML(row,i,field,label)}</label>`).join('')}</details></td></tr>`).join('')}</tbody></table>` : `<p class="empty">${doc.rows.length ? '没有匹配的条目。' : name === 'terms' ? '上传语料后提取需要统一的专业术语。' : name === 'people' ? '尚无人名。可手动添加、批量导入或在翻译中积累。' : '还没有翻译对照。完成翻译并通过审校后会自动积累，也可以手动添加。'}</p>`;
  if (name === 'mappings') {
    $('legacy-terms').hidden = !doc.legacy?.trim() || !doc.rows.some(r => r.source?.includes('旧关键词')) && doc.legacy.trim() === '# 关键词对照\n\n| 原文 | 译文 | 说明 |\n| --- | --- | --- |';
    $('legacy-terms-content').textContent = doc.legacy || '';
  }
}
function setTermDocument(name, doc, redraw = false) {
  $(name + '-editor').value = JSON.stringify(doc, null, 2) + '\n';
  const editor = state.editors[name];
  editor.dirty = true; clearTimeout(editor.timer); editorStatus(name, '待保存', 'dirty');
  if (redraw) renderTerminology(name);
  const complete = doc.rows.every(r => name === 'terms' ? r.term.trim() : r.original.trim() && (name === 'people' && r.status !== 'active' || r.translation.trim()));
  if (complete) editor.timer = setTimeout(() => saveConfig(name).catch(e => toast(e.message, true)), 700);
  else editorStatus(name, '请填写术语 / 译法', 'dirty');
}
$('term-search').addEventListener('input', () => renderTerminology(currentTermTab));
$('term-filter').addEventListener('change', () => renderTerminology(currentTermTab));
$('terminology-card').addEventListener('input', e => {
  const name = e.target.dataset.termKind; if (!name || !e.target.dataset.field) return;
  const doc = JSON.parse($(name + '-editor').value);
  const row = doc.rows[Number(e.target.dataset.row)];
  row[e.target.dataset.field] = e.target.value; row.origin = 'manual';
  if (name === 'people' && !row.translation.trim()) row.status = 'pending';
  const tr = e.target.closest('tr'); const meta = tr.classList.contains('term-meta') ? tr : tr.nextElementSibling;
  const badge = meta.querySelector('.term-origin'); badge.textContent = '用户指定'; badge.classList.add('manual');
  const status = meta.querySelector('.term-state'); status.textContent = termStatuses[row.status || 'active']; status.className = `term-state ${row.status || 'active'}`;
  setTermDocument(name, doc);
});
$('terminology-card').addEventListener('click', async e => {
  const tab = e.target.closest('[data-term-tab]'); if (tab) { showTermTab(tab.dataset.termTab); return; }
  const resolve = e.target.closest('[data-term-resolve]');
  if (resolve) {
    const name = resolve.dataset.termKind; const pid = state.project.id;
    try {
      await flushEditors();
      if (state.project?.id !== pid) return;
      const row = JSON.parse($(name + '-editor').value).rows[Number(resolve.dataset.row)];
      if (resolve.dataset.termResolve === 'activate' && row.conflict && !confirm('确认采用此候选写法，替换同语境下的现有条目？')) return;
      const data = await api(`/api/projects/${pid}/terminology/${name}/resolve`, jsonOptions('POST', {revision:state.editors[name].revision,row_index:Number(resolve.dataset.row),action:resolve.dataset.termResolve}));
      if (state.project?.id !== pid) return;
      $(name + '-editor').value = data.content; state.editors[name].revision = data.revision; renderTerminology(name);
    } catch (error) { toast(error.message, true); }
    return;
  }
  const add = e.target.closest('[data-add-term]');
  const remove = e.target.closest('[data-delete-term]');
  const name = add?.dataset.addTerm || remove?.dataset.deleteTerm; if (!name) return;
  try {
    const doc = JSON.parse($(name + '-editor').value);
    if (add) { const row = Object.fromEntries([...termFields[name],...termDetails[name]].map(([field])=>[field,''])); row.source='手动添加'; row.origin='manual'; row.status=name === 'people' ? 'pending' : 'active'; doc.rows.push(row); }
    else doc.rows.splice(Number(remove.dataset.row),1);
    $('term-filter').value = 'all'; $('term-search').value = '';
    setTermDocument(name, doc, true);
    if (add) $(name + '-table').querySelector(`textarea[data-row="${doc.rows.length - 1}"]`)?.focus();
  } catch (error) { toast(error.message || '请先修正条目或重新载入', true); }
});
document.querySelector('.term-tabs').addEventListener('keydown', e => {
  if (['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) {
    e.preventDefault();
    const tabs = ['terms','mappings','people']; const index = tabs.indexOf(currentTermTab);
    const name = e.key === 'Home' ? tabs[0] : e.key === 'End' ? tabs[2] : tabs[(index + (e.key === 'ArrowLeft' ? 2 : 1)) % 3];
    showTermTab(name); $('tab-' + name).focus();
  }
});

$('corpus-list').addEventListener('click', async e => {
  const button = e.target.closest('[data-delete-corpus]'); if (!button) return;
  const pid = state.project.id;
  const file = state.files.find(f => f.id === button.dataset.deleteCorpus); if (!file) return;
  if (!confirm(`删除参考语料“${file.name}”？\n后续 RAG 不再召回此文档，已有风格、术语及历史记录保留。`)) return;
  button.disabled = true;
  try {
    await api(`/api/projects/${pid}/files/${file.id}`, {method:'DELETE'});
    if (state.project?.id === pid) { $('recall-results').innerHTML = ''; await refreshProject(); }
    toast('参考语料已删除，索引将在下次使用时自动重建');
  } catch (error) { toast(error.message, true); button.disabled = false; }
});
let deletingProject = null;
bind('delete-workspace', async () => {
  await flushEditors();
  deletingProject = {...state.project};
  $('delete-workspace-confirm').value = '';
  $('delete-workspace-name').textContent = `工作空间：${deletingProject.name}`;
  $('delete-workspace-error').textContent = '';
  $('confirm-delete-workspace').disabled = true;
  $('delete-workspace-dialog').showModal();
  $('delete-workspace-confirm').focus();
});
$('delete-workspace-confirm').addEventListener('input', () => {
  $('confirm-delete-workspace').disabled = $('delete-workspace-confirm').value !== deletingProject?.name;
});
$('cancel-delete-workspace').addEventListener('click', () => $('delete-workspace-dialog').close());
$('delete-workspace-form').addEventListener('submit', async e => {
  e.preventDefault();
  const project = deletingProject;
  if (!project || $('delete-workspace-confirm').value !== project.name) return;
  $('confirm-delete-workspace').disabled = true;
  try {
    await api(`/api/projects/${project.id}`, jsonOptions('DELETE', {name:project.name}));
    $('delete-workspace-dialog').close();
    state.projects = state.projects.filter(p => p.id !== project.id);
    if (state.project?.id === project.id) {
      state.stream?.close(); state.stream = null;
      clearTimeout(state.refreshTimer);
      for (const editor of Object.values(state.editors)) clearTimeout(editor.timer);
      state.editors = {}; state.project = null; state.files = []; state.jobs = []; state.artifacts = [];
      localStorage.removeItem('transmux.project');
      if (state.projects.length) await selectProject(state.projects[0].id);
      else {
        $('workspace').hidden = true; $('onboarding').hidden = false;
        $('agent-pill').textContent = '尚未绑定 Agent'; $('agent-pill').setAttribute('aria-disabled','true');
        $('model-menu').open = false; $('breadcrumb-name').textContent = '新项目';
      }
    }
    drawProjects(); toast('工作空间已删除');
  } catch (error) { $('delete-workspace-error').textContent = error.message; }
  finally { $('confirm-delete-workspace').disabled = $('delete-workspace-confirm').value !== deletingProject?.name; }
});
