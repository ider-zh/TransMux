let comparisonView = null;
let comparisonRequest = 0;

function closeComparison() {
  if ($('comparison-dialog').open) $('comparison-dialog').close();
}
$('close-comparison').addEventListener('click', closeComparison);
$('comparison-dialog').addEventListener('close', () => {
  const scroll = comparisonView?.workspaceScroll;
  comparisonRequest++;
  comparisonView = null;
  document.body.classList.remove('comparison-open');
  if (scroll !== undefined) window.scrollTo(0, scroll);
});

function comparisonAnchor() {
  const body = $('comparison-body');
  const top = body.getBoundingClientRect().top;
  const row = [...body.querySelectorAll('.comparison-pair')].find(el => el.getBoundingClientRect().bottom > top);
  return row ? {id:row.id, offset:row.getBoundingClientRect().top - top} : null;
}

async function openComparison(fid, preservePosition = false) {
  const previous = comparisonView;
  const anchor = preservePosition ? comparisonAnchor() : null;
  const pid = state.project.id;
  const request = ++comparisonRequest;
  const view = {pid, fid, data:null, loading:true, workspaceScroll:previous?.workspaceScroll ?? window.scrollY};
  comparisonView = view;
  $('comparison-error').textContent = '';
  $('comparison-status').textContent = '正在载入段落对照…';
  $('comparison-version').disabled = true;
  $('comparison-download').hidden = true;
  $('comparison-update').hidden = true;
  if (!$('comparison-dialog').open) {
    $('comparison-body').innerHTML = '';
    $('comparison-source').textContent = '';
    $('comparison-count').textContent = '';
    $('comparison-version').innerHTML = '';
    $('comparison-version').dataset.signature = '';
    document.body.classList.add('comparison-open');
    $('comparison-dialog').showModal();
  }
  $('comparison-body').querySelectorAll('button,textarea').forEach(el => el.disabled = true);
  try {
    const data = await api(`/api/projects/${pid}/files/${fid}/comparison`);
    if (request !== comparisonRequest || comparisonView !== view || state.project?.id !== pid) return;
    view.data = data; view.loading = false;
    $('comparison-source').textContent = `原文：${data.source_name}`;
    $('comparison-count').textContent = `${data.paragraphs.length} 组 · 原文 ${data.paragraphs.reduce((n, r) => n + (r.original_paragraphs?.length || 1), 0)} 段 → 译文 ${data.paragraphs.reduce((n, r) => n + (r.translations?.length || 1), 0)} 段`;
    $('comparison-download').href = `/api/projects/${pid}/files/${fid}/download`;
    $('comparison-download').hidden = false;
    $('comparison-body').innerHTML = data.paragraphs.map((row, index) => {
      const number = index + 1;
      const location = row.kind === 'table' ? ` · 表格 ${row.table} · 行 ${row.row} · 列 ${row.column}` : row.kind === 'heading' ? ` · ${row.level} 级标题` : '';
      const originals = row.original_paragraphs || [row.original];
      const targets = row.translations || [row.translation];
      const grouped = originals.length !== 1 || targets.length !== 1;
      const paragraphHTML = (texts, positions, side) => texts.map((text, i) => `<div class="comparison-text">${texts.length > 1 ? `<small>${`${side}第 ${positions?.[i] || i + 1} 段`}</small>` : ''}${escapeHTML(text)}</div>`).join('');
      const reason = row.reason ? `<p class="comparison-group-reason">${escapeHTML(row.reason)}</p>` : '';
      return `<article class="comparison-pair ${row.kind === 'heading' ? 'is-heading' : ''}" id="compare-paragraph-${number}" data-paragraph="${number}"><div class="comparison-paragraph-label">第 ${number} 组${location}${grouped ? ` · ${originals.length > targets.length ? '合并' : originals.length < targets.length ? '拆分' : '重组'} · ${originals.length} → ${targets.length} 段` : ''}</div>${reason}<div class="comparison-columns"><section class="comparison-original" aria-label="第 ${number} 段原文"><span class="comparison-side-label">原文</span>${paragraphHTML(originals, row.source_positions, '原文')}</section><section class="comparison-translation" aria-label="第 ${number} 段译文"><span class="comparison-side-label">译文</span>${paragraphHTML(targets, null, '本组译文')}<button class="text-button" data-revise-paragraph="${number}">修改此组</button><form class="paragraph-revision" data-revision-form="${number}" hidden><label class="field">修改要求<textarea rows="3" maxlength="20000" required aria-label="第 ${number} 段修改要求" placeholder="例如：更简洁，保留专业术语"></textarea></label><p>仅修改此组；可要求恢复原文分段。审校通过后生成新版本。</p><div class="button-row"><button type="submit" class="primary">提交修改</button><button type="button" class="text-button" data-cancel-revision="${number}">取消</button></div><p class="revision-message" role="status"></p></form></section></div></article>`;
    }).join('');
    $('comparison-body').scrollTop = 0;
    refreshComparisonState();
    if (anchor) {
      const row = $(anchor.id);
      if (row) $('comparison-body').scrollTop += row.getBoundingClientRect().top - $('comparison-body').getBoundingClientRect().top - anchor.offset;
    }
  } catch (error) {
    if (request !== comparisonRequest) return;
    // Keep the old version usable when a version switch fails.
    if (previous?.data && previous.pid === pid) {
      comparisonView = previous;
      refreshComparisonState();
      $('comparison-download').href = `/api/projects/${pid}/files/${previous.fid}/download`;
      $('comparison-download').hidden = false;
    } else { view.loading = false; $('comparison-status').textContent = ''; }
    $('comparison-error').textContent = error.message;
  }
}

function refreshComparisonState() {
  const view = comparisonView;
  if (!view?.data || view.loading || state.project?.id !== view.pid) return;
  const versions = new Map(view.data.versions.map(file => [file.id, file]));
  for (const file of state.files) {
    if (file.root_file === view.data.root_file && !versions.has(file.id)) versions.set(file.id, file);
  }
  view.versions = [...versions.values()].sort((a, b) => a.created - b.created);
  const signature = view.versions.map(file => file.id).join(',');
  if ($('comparison-version').dataset.signature !== signature) {
    $('comparison-version').innerHTML = view.versions.map((file, i) => `<option value="${file.id}">版本 ${i + 1}${file.parent_file ? ' · 段落修订' : ' · 完整翻译'} · ${escapeHTML(new Date(file.created * 1000).toLocaleString('zh-CN'))}</option>`).join('');
    $('comparison-version').dataset.signature = signature;
  }
  $('comparison-version').value = view.fid;
  $('comparison-version').disabled = false;
  view.latest = view.versions.at(-1).id;
  const relevantJobs = state.jobs.filter(job => {
    if (job.kind !== 'revise') return false;
    try { return versions.has(JSON.parse(job.payload).file_id); } catch { return false; }
  });
  const active = relevantJobs.find(job => ['queued','running'].includes(job.state));
  const latestJob = active || relevantJobs[0];
  view.locked = Boolean(active || view.submitting || view.latest !== view.fid);
  $('comparison-update').hidden = view.latest === view.fid;
  $('comparison-body').querySelectorAll('[data-revise-paragraph],.paragraph-revision button[type=submit]').forEach(button => {
    button.disabled = view.locked;
    button.title = active ? '当前修改任务完成后，请切换新版本再修改' : view.latest !== view.fid ? '请先切换新版本' : '';
  });
  $('comparison-body').querySelectorAll('textarea,[data-cancel-revision]').forEach(el => el.disabled = false);
  let status = '按段落组对照，支持合并与拆分；修改进入本项目队列，仅审校所选组。';
  if (active) {
    const progress = active.progress ? JSON.parse(active.progress) : {};
    status = active.state === 'queued' ? '段落修改已排队，等待本项目当前任务完成。' : `${progress.title || 'Agent 处理中'} · ${progress.detail || '正在处理所选段落'}`;
  } else if (latestJob && ['failed','needs_attention','interrupted','cancelled'].includes(latestJob.state)) {
    status = `上次段落修改${statuses[latestJob.state]}：${latestJob.result || ''}。当前版本保留，草稿可在任务记录中查看。`;
  } else if (view.latest !== view.fid) status = '新版本已通过审校。点击“查看新版本”切换，当前阅读位置会保留。';
  $('comparison-status').textContent = status;
  $('comparison-status').dataset.state = active?.state || latestJob?.state || 'idle';
}

$('file-list').addEventListener('click', e => {
  const button = e.target.closest('[data-compare]');
  if (button) openComparison(button.dataset.compare);
});
$('comparison-version').addEventListener('change', e => openComparison(e.target.value, true));
$('comparison-view-latest').addEventListener('click', () => {
  if (comparisonView?.latest) openComparison(comparisonView.latest, true);
});
$('comparison-body').addEventListener('click', e => {
  const edit = e.target.closest('[data-revise-paragraph]');
  const cancel = e.target.closest('[data-cancel-revision]');
  const number = edit?.dataset.reviseParagraph || cancel?.dataset.cancelRevision;
  if (!number) return;
  const form = $('comparison-body').querySelector(`[data-revision-form="${number}"]`);
  form.hidden = Boolean(cancel);
  if (edit) form.querySelector('textarea').focus();
});
$('comparison-body').addEventListener('submit', async e => {
  const form = e.target.closest('[data-revision-form]'); if (!form) return;
  e.preventDefault();
  const view = comparisonView;
  if (!view?.data || view.locked) return;
  const message = form.querySelector('textarea').value.trim(); if (!message) return;
  const status = form.querySelector('.revision-message');
  view.submitting = true; refreshComparisonState();
  try {
    await flushEditors();
    const job = await api(`/api/projects/${view.pid}/jobs`, jsonOptions('POST', {
      kind:'revise', file_id:view.fid, paragraph:Number(form.dataset.revisionForm), message,
      use_rag:$('use-rag').checked, max_review_rounds:Number($('review-rounds').value),
    }));
    if (state.project?.id === view.pid) { state.jobs.unshift(job); renderJobs(); }
    status.textContent = '已提交。审校通过后会提示查看新版本，当前版本保持不变。';
    scheduleRefresh();
  } catch (error) { status.textContent = error.message; }
  finally { view.submitting = false; refreshComparisonState(); }
});
