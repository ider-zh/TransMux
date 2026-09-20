let layoutView = null;

function setLayoutPanel(panel) {
  if (layoutView) layoutView.panel = panel;
  for (const name of ['preview', 'structure', 'report']) {
    const selected = name === panel;
    $('layout-' + name).hidden = !selected;
    $('layout-tab-' + name).setAttribute('aria-selected', String(selected));
    $('layout-tab-' + name).tabIndex = selected ? 0 : -1;
  }
}
for (const tab of document.querySelectorAll('[data-layout-panel]')) {
  tab.addEventListener('click', () => setLayoutPanel(tab.dataset.layoutPanel));
  tab.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll('[data-layout-panel]')].filter(item => !item.hidden);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    setLayoutPanel(tabs[next].dataset.layoutPanel);
    tabs[next].focus();
  });
}

function closeLayout() {
  if (!$('layout-dialog').open) return;
  $('layout-dialog').close();
}
$('close-layout').addEventListener('click', closeLayout);
$('layout-dialog').addEventListener('close', () => {
  const scroll = layoutView?.scroll;
  layoutView = null;
  $('layout-pdf').removeAttribute('src');
  document.body.classList.remove('layout-open');
  if (scroll !== undefined) window.scrollTo(0, scroll);
});

async function openLayout(fid) {
  if (state.pendingUpgrade) throw new Error('服务正在升级，请稍后打开排版与导出');
  const file = state.files.find(f => f.id === fid);
  if (!file?.can_layout) throw new Error('请选择 DOCX 文稿或译文');
  const view = {pid:state.project.id, fid, rows:[], selected:'', signature:null, loading:false, submitting:false, roles:{}, structure:null, scroll:window.scrollY};
  layoutView = view;
  $('layout-template').value = 'original';
  $('layout-structure').hidden = true;
  $('layout-tab-structure').hidden = true;
  setLayoutPanel('preview');
  $('layout-structure-list').replaceChildren();
  $('layout-template-note').textContent = '保留原格式，不修改排版或引文。';
  $('layout-source').textContent = file.name;
  $('layout-original').href = `/api/projects/${view.pid}/files/${fid}/download`;
  $('layout-version').innerHTML = '<option value="">读取导出历史…</option>';
  $('layout-error').textContent = '';
  $('layout-checks').innerHTML = '<p class="layout-note">生成导出版本后，这里会显示检查结果。</p>';
  $('layout-downloads').hidden = true;
  $('layout-open-pdf').hidden = true;
  $('layout-pdf').hidden = true;
  $('layout-pdf').removeAttribute('src');
  $('layout-empty').hidden = false;
  $('layout-empty').textContent = '点击“生成预览与导出版本”，查看 PDF。原 DOCX 可直接下载。';
  if (!$('layout-dialog').open) $('layout-dialog').showModal();
  document.body.classList.add('layout-open');
  refreshLayoutState();
}

document.addEventListener('click', e => {
  const button = e.target.closest('[data-layout]');
  if (button) openLayout(button.dataset.layout).catch(error => toast(error.message, true));
});

function layoutJobs(view) {
  return state.jobs.filter(job => {
    if (job.kind !== 'layout' || job.project !== view.pid) return false;
    try { return JSON.parse(job.payload).file_id === view.fid; } catch { return false; }
  });
}

function refreshLayoutState() {
  const view = layoutView;
  if (!view || state.project?.id !== view.pid) return;
  const jobs = layoutJobs(view);
  const active = jobs.find(job => ['running','queued'].includes(job.state));
  const latest = active || jobs[0];
  $('layout-generate').disabled = !!active || view.submitting || state.pendingUpgrade || ($('layout-template').value !== 'original' && !view.structure);
  $('layout-status').dataset.state = active?.state || latest?.state || 'idle';
  let text = '生成任务沿用本项目队列，完成后可在这里查看导出版本。';
  if (active?.state === 'queued') text = '已排队 · 等待本项目当前任务完成';
  else if (active?.state === 'running') {
    const progress = active.progress ? JSON.parse(active.progress) : {};
    text = `${progress.title || '准备导出'} · ${progress.detail || '正在读取文稿'}`;
  } else if (latest) {
    try { text = JSON.parse(latest.result).message || `排版与导出 · ${statuses[latest.state]}`; }
    catch { text = latest.result || `排版与导出 · ${statuses[latest.state]}`; }
  }
  $('layout-status').textContent = text;
  const signature = jobs.map(job => `${job.id}:${job.state}`).join('|');
  if (view.signature !== signature && !view.loading) loadLayoutVersions(view, signature);
}

async function loadLayoutVersions(view, signature) {
  view.loading = true;
  try {
    const rows = await api(`/api/projects/${view.pid}/files/${view.fid}/exports`);
    if (layoutView !== view) return;
    view.rows = rows;
    view.signature = signature;
    if (!view.selected || !rows.some(row => row.id === view.selected) || view.wantNewest && rows[0]?.id !== view.previousNewest) {
      view.selected = rows[0]?.id || '';
      if (rows[0]?.id !== view.previousNewest) view.wantNewest = false;
    }
    $('layout-version').innerHTML = rows.length ? rows.map((row, i) => `<option value="${row.id}">版本 ${rows.length-i} · ${escapeHTML(row.manifest.template.name)} · ${escapeHTML(new Date(row.created*1000).toLocaleString('zh-CN'))} · ${row.manifest.pdf ? 'DOCX + PDF' : '仅 DOCX'}</option>`).join('') : '<option value="">尚无导出版本</option>';
    $('layout-version').value = view.selected;
    renderLayoutVersion();
  } catch (error) {
    if (layoutView === view) $('layout-error').textContent = error.message;
  } finally { view.loading = false; }
}

function renderLayoutVersion() {
  const view = layoutView;
  if (!view) return;
  const row = view.rows.find(row => row.id === view.selected);
  if (!row) return;
  const manifest = row.manifest;
  const base = `/api/projects/${view.pid}/exports/${row.id}`;
  $('layout-downloads').hidden = false;
  $('layout-docx').href = base + '/docx';
  $('layout-manifest').href = base + '/json';
  $('layout-pdf-download').hidden = !manifest.pdf;
  $('layout-pdf-download').href = base + '/pdf';
  $('layout-open-pdf').hidden = !manifest.pdf;
  $('layout-open-pdf').href = base + '/pdf?preview=true';
  $('layout-empty').hidden = manifest.pdf;
  $('layout-pdf').hidden = !manifest.pdf;
  if (manifest.pdf) {
    const url = base + '/pdf?preview=true#view=FitH&navpanes=0';
    if ($('layout-pdf').getAttribute('src') !== url) $('layout-pdf').src = url;
  } else {
    $('layout-pdf').removeAttribute('src');
    $('layout-empty').textContent = '此版本已保留 DOCX，PDF 未生成。请查看“检查结果”。';
  }
  $('layout-checks').innerHTML = `<h3>${escapeHTML(manifest.template.name)}</h3><p>${manifest.pdf ? `PDF 已生成 · ${manifest.renderer.pages} 页` : 'PDF 需要处理'}</p>` + manifest.checks.map(check => `<p class="layout-check">✓ ${escapeHTML(check.message)}</p>`).join('') + manifest.issues.map(issue => `<p class="layout-issue">${escapeHTML(issue)}</p>`).join('') + `<p class="layout-note">${escapeHTML(manifest.notice)}</p>`;
  if (manifest.findings) {
    const existing = $('layout-checks').querySelectorAll('.layout-issue');
    const offset = manifest.issues.length - manifest.findings.length;
    manifest.findings.forEach((finding, index) => {
      if (!finding.block_id) return;
      const block = manifest.blocks.find(block => block.id === finding.block_id);
      if (!block) return;
      const details = document.createElement('details');
      const summary = document.createElement('summary'); summary.textContent = `查看位置 · 文档块 ${block.position}`;
      const text = document.createElement('p'); text.textContent = block.text || '图片、表格或其他对象';
      details.append(summary, text); existing[offset + index]?.append(details);
    });
    const source = document.createElement('a');
    source.href = manifest.template.guidelines_url;
    source.target = '_blank'; source.rel = 'noopener'; source.className = 'text-button';
    source.textContent = `官方模板依据 · ${manifest.template.version}`;
    $('layout-checks').append(source);
  }
}

$('layout-version').addEventListener('change', () => {
  if (!layoutView) return;
  layoutView.selected = $('layout-version').value;
  layoutView.wantNewest = false;
  renderLayoutVersion();
});

$('layout-generate').addEventListener('click', async () => {
  const view = layoutView;
  if (!view || view.submitting || state.pendingUpgrade) return;
  view.submitting = true;
  $('layout-error').textContent = '';
  refreshLayoutState();
  try {
    const template = $('layout-template').value;
    const extra = template === 'original' ? {} : {source_sha256:view.structure.source_sha256, roles:{...view.roles}};
    const job = await api(`/api/projects/${view.pid}/jobs`, jsonOptions('POST', {kind:'layout', file_id:view.fid, template, ...extra}));
    if (state.project?.id === view.pid) {
      state.jobs.unshift(job);
      view.previousNewest = view.rows[0]?.id;
      view.wantNewest = true;
      if (layoutView === view) setLayoutPanel('preview');
      renderJobs();
      await refreshProject();
    }
  } catch (error) {
    if (layoutView === view) $('layout-error').textContent = error.message;
  } finally {
    view.submitting = false;
    if (layoutView === view) refreshLayoutState();
  }
});

function renderLayoutStructure(view) {
  $('layout-structure-list').innerHTML = view.structure.blocks.map(block => `<div class="layout-block"><small>文档块 ${block.position}${block.inferred && !view.roles[block.id] ? ' · 推测，请确认' : ''}</small><p>${escapeHTML(block.text || '图片、表格或空段落')}</p>${block.kind === 'paragraph' ? `<select data-layout-role="${block.id}" aria-label="文档块 ${block.position} 类型">${Object.entries(view.structure.roles).map(([value,label]) => `<option value="${value}" ${value === (view.roles[block.id] || block.role) ? 'selected' : ''}>${escapeHTML(label)}</option>`).join('')}</select>` : '<small>对象保留原样，请检查双栏中的尺寸</small>'}</div>`).join('');
}
$('layout-template').addEventListener('change', async () => {
  const view = layoutView;
  if (!view) return;
  const template = $('layout-template').value;
  const preset = template !== 'original';
  $('layout-template-note').textContent = template === 'jcst' ? '使用 JCST 官方出版 Word 模板的格式规则。引文与编号暂沿用原稿，输出为待核对的出版排版草稿。' : preset ? '使用 IEEE Access 官方 2024 Word 模板的格式规则。引文与编号暂沿用原稿，输出为待核对的投稿草稿。' : '保留原格式，不修改排版或引文。';
  $('layout-tab-structure').hidden = !preset;
  if (!preset && view.panel === 'structure') setLayoutPanel('preview');
  refreshLayoutState();
  if (!preset || view.structure) return;
  $('layout-structure-list').textContent = '正在识别文档结构…';
  try {
    const result = await api(`/api/projects/${view.pid}/files/${view.fid}/structure`);
    if (layoutView !== view) return;
    view.structure = result;
    renderLayoutStructure(view);
  } catch (error) {
    if (layoutView === view) { $('layout-error').textContent = error.message; $('layout-structure-list').textContent = '读取失败，请重新打开面板。'; }
  } finally { if (layoutView === view) refreshLayoutState(); }
});
$('layout-structure-list').addEventListener('change', event => {
  if (!layoutView || !event.target.dataset.layoutRole) return;
  layoutView.roles[event.target.dataset.layoutRole] = event.target.value;
});
$('layout-confirm-structure').addEventListener('click', () => {
  const view = layoutView;
  if (!view?.structure) return;
  for (const block of view.structure.blocks) if (block.inferred) view.roles[block.id] ||= block.role;
  renderLayoutStructure(view);
});

$('manuscript-upload').addEventListener('change', async event => {
  const input = event.target;
  const file = input.files[0];
  if (!file || !state.project) return;
  const pid = state.project.id;
  input.disabled = true;
  $('manuscript-upload-status').textContent = '正在上传和解析文稿…';
  try {
    if (state.pendingUpgrade) throw new Error('服务正在升级，请稍后上传');
    const body = new FormData(); body.append('file', file);
    const uploaded = await api(`/api/projects/${pid}/files?kind=manuscript`, {method:'POST', body});
    if (state.project?.id !== pid) return;
    await refreshProject();
    if (state.project?.id !== pid) return;
    $('manuscript-upload-status').textContent = '文稿已上传，可直接排版与导出。';
    await openLayout(uploaded.id);
  } catch (error) {
    if (state.project?.id === pid) $('manuscript-upload-status').textContent = error.message;
  } finally { input.disabled = false; input.value = ''; }
});
