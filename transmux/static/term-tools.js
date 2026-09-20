let termImport = null;
let termReview = null;
const termKindLabels = {terms:'专业术语', mappings:'翻译对照', people:'人名'};
function closeTermTools() {
  for (const id of ['term-import-dialog','term-review-dialog','terms-expanded']) if ($(id).open) $(id).close();
}
bind('expand-terminology', () => {
  const card = $('terminology-card');
  const placeholder = document.createElement('div'); placeholder.id = 'term-home';
  card.before(placeholder); $('term-expansion-body').append(card);
  $('expand-terminology').hidden = true; $('terms-expanded').showModal();
});
$('close-terms-expanded').onclick = () => $('terms-expanded').close();
$('terms-expanded').addEventListener('close', () => {
  $('term-home')?.replaceWith($('terminology-card')); $('expand-terminology').hidden = false;
});
$('close-term-import').onclick = () => $('term-import-dialog').close();
$('term-import-dialog').addEventListener('close', () => { termImport = null; });
$('close-term-review').onclick = () => $('term-review-dialog').close();
$('term-review-dialog').addEventListener('close', () => { termReview = null; });

$('terminology-card').addEventListener('click', async e => {
  const button = e.target.closest('[data-import-terms]'); if (!button) return;
  const pid = state.project.id;
  try { await flushEditors(); } catch (error) { toast(error.message,true); return; }
  if (state.project?.id !== pid) return;
  termImport = {pid, kind:button.dataset.importTerms, cells:[], items:[], page:0};
  $('term-import-title').textContent = termKindLabels[termImport.kind] + ' · 批量导入';
  for (const id of ['term-import-columns','term-import-rows','term-import-info','term-import-error']) $(id).textContent = '';
  $('term-import-paste').value = ''; $('term-import-file').value = '';
  $('term-import-preview').hidden = true; $('term-import-apply').hidden = true;
  $('term-import-dialog').showModal();
});
function importFields(kind) { return [...termFields[kind], ...termDetails[kind]]; }
bind('term-import-template', () => {
  const header = importFields(termImport.kind).map(([,label]) => label).join(',');
  const url = URL.createObjectURL(new Blob(['\ufeff' + header + '\r\n'], {type:'text/csv;charset=utf-8'}));
  const link = document.createElement('a'); link.href = url; link.download = termKindLabels[termImport.kind] + '-模板.csv'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
function importColumns() {
  const view = termImport; if (!view?.cells.length) return;
  const header = $('term-import-header').checked;
  const width = Math.max(...view.cells.map(row => row.length));
  const labels = Array.from({length:width}, (_, i) => header ? view.cells[0][i] || `第 ${i+1} 列` : `第 ${i+1} 列`);
  $('term-import-columns').innerHTML = '<p>选择每个字段对应的表格列。不需要的字段可以忽略。</p><div class="term-column-map">' + importFields(view.kind).map(([field,label], index) => {
    const aliases = {term:['术语','首选术语'],original:['原文','原文人名','原文术语'],translation:['译文','标准写法','目标译法'],scope:['语境','适用语境'],context:['语境','适用语境','身份 / 适用语境']};
    const found = labels.findIndex(value => [field,label,...(aliases[field] || [])].map(x => x.toLowerCase()).includes(value.trim().toLowerCase()));
    const selected = found >= 0 ? found : !header && index < width ? index : -1;
    return `<label class="field">${escapeHTML(label)}<select data-import-column="${field}"><option value="-1">忽略 / 留空</option>${labels.map((value, i) => `<option value="${i}" ${i === selected ? 'selected' : ''}>${escapeHTML(value)}</option>`).join('')}</select></label>`;
  }).join('') + '</div>';
  $('term-import-preview').hidden = false; $('term-import-apply').hidden = true; $('term-import-rows').textContent = '';
}
$('term-import-header').addEventListener('change', importColumns);
bind('term-import-parse', async () => {
  const view = termImport; if (!view) return;
  const form = new FormData();
  const file = $('term-import-file').files[0];
  if (file) form.append('file', file); else form.append('text', $('term-import-paste').value);
  $('term-import-error').textContent = '';
  try {
    const data = await api(`/api/projects/${view.pid}/terminology/parse`, {method:'POST',body:form});
    if (termImport !== view) return;
    view.cells = data.cells; importColumns();
    $('term-import-info').textContent = `读取 ${data.cells.length} 行${data.sheet ? ` · 工作表：${data.sheet}` : ''}。请检查列对应关系。`;
  } catch (error) { if (termImport === view) $('term-import-error').textContent = error.message; }
});
bind('term-import-preview', async () => {
  const view = termImport; if (!view) return;
  const columns = [...$('term-import-columns').querySelectorAll('select')].map(el => [el.dataset.importColumn, Number(el.value)]);
  const rows = view.cells.slice($('term-import-header').checked ? 1 : 0).map(cells => Object.fromEntries(columns.filter(([,n])=>n >= 0).map(([field,n])=>[field,cells[n] || ''])));
  try {
    const result = await api(`/api/projects/${view.pid}/terminology/${view.kind}/preview`, jsonOptions('POST', {rows}));
    if (termImport !== view) return;
    view.revision = result.revision; view.items = result.items.map(item => ({...item, choice:item.action === 'add' ? 'add' : 'skip'})); view.page = 0;
    renderImportPreview(); $('term-import-apply').hidden = false; $('term-import-error').textContent = '';
  } catch (error) { if (termImport === view) $('term-import-error').textContent = error.message; }
});
function editablePreview(row, fields, prefix, index) {
  return fields.map(([field,label])=>`<label class="field">${escapeHTML(label)}<textarea rows="2" data-${prefix}-index="${index}" data-${prefix}-field="${field}">${escapeHTML(row[field] || '')}</textarea></label>`).join('');
}
function renderImportPreview() {
  const view = termImport;
  const start = view.page * 50;
  const labels = {add:'新增',duplicate:'完全重复，默认跳过',conflict:'冲突，默认跳过',error:'需修正'};
  $('term-import-rows').innerHTML = `<p>共 ${view.items.length} 条。可编辑后导入；重复与冲突不会默认覆盖。</p><div class="button-row"><button class="text-button" data-import-page="-1" ${view.page === 0 ? 'disabled' : ''}>上一页</button><span>${view.page + 1} / ${Math.max(1,Math.ceil(view.items.length / 50))}</span><button class="text-button" data-import-page="1" ${start + 50 >= view.items.length ? 'disabled' : ''}>下一页</button></div>` + view.items.slice(start,start+50).map((item,i)=> {
    const index = start + i;
    return `<article class="term-preview-item"><div class="term-preview-heading"><b>第 ${index+1} 条 · ${labels[item.action]}</b><select aria-label="第 ${index+1} 条导入操作" data-import-choice="${index}">${[['skip','跳过'],['add','新增'],['replace','采用此内容（替换同词同语境条目）']].map(([value,label])=>`<option value="${value}" ${item.choice === value ? 'selected' : ''}>${label}</option>`).join('')}</select></div>${item.error ? `<p class="error-text">${escapeHTML(item.error)}</p>` : ''}${item.existing ? `<p>已有内容：${escapeHTML(importFields(view.kind).map(([f,label])=>`${label}：${item.existing[f] || ''}`).join('；'))}</p>` : ''}<div class="term-preview-fields">${editablePreview(item.row,importFields(view.kind),'import',index)}</div></article>`;
  }).join('');
}
$('term-import-rows').addEventListener('input', e => {
  if (e.target.dataset.importField) termImport.items[Number(e.target.dataset.importIndex)].row[e.target.dataset.importField] = e.target.value;
  if (e.target.dataset.importChoice !== undefined) termImport.items[Number(e.target.dataset.importChoice)].choice = e.target.value;
});
$('term-import-rows').addEventListener('click', e => {
  const button = e.target.closest('[data-import-page]'); if (button) { termImport.page += Number(button.dataset.importPage); renderImportPreview(); }
});
bind('term-import-apply', async () => {
  const view = termImport; if (!view) return;
  try {
    await flushEditors();
    await api(`/api/projects/${view.pid}/terminology/${view.kind}/import`, jsonOptions('POST', {revision:view.revision, operations:view.items.map(item=>({action:item.choice,row:item.row}))}));
    if (termImport !== view) return;
    await loadConfig(view.kind,true); $('term-import-dialog').close(); toast('已导入，历史快照已保存');
  } catch (error) { if (termImport === view) $('term-import-error').textContent = error.message; }
});
bind('rescreen-terms', async () => {
  await submitJob('terminology_review', {});
  toast('已提交重新筛选；完成后点击“查看筛选建议”。当前条目不会自动更改。');
});
bind('preview-rescreen', async () => {
  const pid = state.project.id;
  await flushEditors();
  if (state.project?.id !== pid) return;
  const job = state.jobs.find(j => j.kind === 'terminology_review');
  if (job && job.state !== 'succeeded') throw new Error('最新筛选尚未完成，请查看任务状态，完成后再预览');
  if (!job) throw new Error('还没有完成的筛选任务，请先点击“按新标准重新筛选”');
  const view = {pid:state.project.id,jid:job.id}; termReview = view;
  const data = await api(`/api/projects/${view.pid}/terminology-review/${view.jid}`);
  if (termReview !== view || state.project.id !== view.pid) return;
  view.items = data.items;
  const labels = {retain:'保留',update:'调整语境 / 写法',person:'归入人名（待确认）',disable:'停用'};
  $('term-review-items').innerHTML = data.items.length ? data.items.map(item=>`<article class="term-preview-item"><label><input type="checkbox" data-review-select="${item.index}" ${item.action !== 'retain' ? 'checked' : ''}>${escapeHTML(item.row.term || item.row.original)} · ${labels[item.action]}</label><p>${escapeHTML(item.reason)}</p><div class="term-preview-fields">${editablePreview(item.proposed,importFields(item.action === 'person' ? 'people' : item.kind),'review',item.index)}</div></article>`).join('') : '<p>没有需要筛选的自动条目，用户指定内容均已保留。</p>';
  $('term-review-error').textContent = ''; $('term-review-apply').disabled = !data.items.length;
  $('term-review-dialog').showModal();
});
bind('term-review-apply', async () => {
  const view = termReview; if (!view) return;
  const decisions = [...$('term-review-items').querySelectorAll('[data-review-select]:checked')].map(input => {
    const index = Number(input.dataset.reviewSelect);
    const edits = Object.fromEntries([...$('term-review-items').querySelectorAll(`[data-review-index="${index}"]`)].map(el=>[el.dataset.reviewField,el.value]));
    return {index,edits};
  });
  try {
    await flushEditors();
    await api(`/api/projects/${view.pid}/terminology-review/${view.jid}/apply`, jsonOptions('POST', {decisions}));
    if (termReview !== view) return;
    await Promise.all(['terms','mappings','people'].map(kind=>loadConfig(kind,true)));
    $('term-review-dialog').close(); toast('已应用所选建议，历史快照已保存');
  } catch (error) { if (termReview === view) $('term-review-error').textContent = error.message; }
});
