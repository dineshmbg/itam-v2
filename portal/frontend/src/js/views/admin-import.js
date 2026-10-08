// Data import: upload a raw file as received, check it (nothing is written), then load it. Administrators only.
import { get, send, upload } from '../core/api.js';
import { h, icon } from '../core/dom.js';

import { toast } from '../ui/toast.js';
import { openModal } from '../ui/modal.js';
import { errorBlock, pageHead, panel } from './common.js';
import { when } from './audit.js';

const STATUS = { UPLOADED: ['mute', 'Staged'], CHECKED: ['info', 'Checked'], CHECK_FAILED: ['bad', 'Check failed'], LOADED: ['ok', 'Loaded'], LOAD_FAILED: ['bad', 'Load failed'] };
const bytes = (n) => (n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);

export function mountImport(root) {
  let dead = false, meta = null, job = null;
  const holder = h('div', { class: 'page' });
  root.append(holder);
  const body = h('div', { class: 'grid' });
  holder.append(pageHead('Data import', 'Upload the file exactly as you received it. The portal applies the same rules used to build the templates: it extracts what is needed, checks it, and only then loads it.'), body);

  const kind = h('select', { id: 'im-kind' });
  const file = h('input', { id: 'im-file', type: 'file', accept: '.xlsx,.xlsm' });
  const asOf = h('input', { id: 'im-asof', type: 'date' });
  const hint = h('div', { class: 'hint' });
  const stepEl = h('div', { class: 'stack-v' });

  async function load() {
    try { meta = await get('/api/admin/import'); if (dead) return; draw(); } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, load)); }
  }

  function draw() {
    kind.replaceChildren(h('option', { value: '' }, 'Choose the kind of file…'), ...meta.kinds.map((k) => h('option', { value: k.key }, k.label)));
    kind.onchange = () => { hint.textContent = meta.kinds.find((k) => k.key === kind.value)?.hint || ''; };
    const f = (id, label, el, h2) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), el, h2 || null);
    body.replaceChildren(
      panel('1 · Upload', { cls: 'span-5' }, h('div', null, f('im-kind', 'What is this file?', kind, hint), f('im-file', 'File (.xlsx)', file), f('im-asof', 'Data as of (optional)', asOf, h('div', { class: 'hint' }, 'Leave empty to use the date in the file or file name.')),
        h('button', { class: 'btn primary', type: 'button', onClick: doUpload }, icon('upload'), 'Upload and check'))),
      panel('2 · Check and load', { cls: 'span-7' }, stepEl),
      panel('History', { cls: 'span-12', flush: true }, history()));
    drawStep();
  }

  async function doUpload() {
    if (!kind.value) { toast('Choose the kind of file first.', 'bad'); return; }
    if (!file.files[0]) { toast('Choose a file to upload.', 'bad'); return; }
    stepEl.replaceChildren(h('div', { class: 'loading-line' }, 'Uploading…'));
    try {
      const up = await upload('/api/admin/import/upload', file.files[0], { 'X-Kind': kind.value });
      job = { ...up, kind: kind.value, log: '', state: 'UPLOADED' };
      if (up.already_loaded) toast('This exact file was already loaded before.');
      drawStep();
      await check();
    } catch (e) { stepEl.replaceChildren(errorBlock(e)); }
  }

  async function check() {
    stepEl.replaceChildren(h('div', { class: 'loading-line' }, 'Checking - the load is rehearsed end to end and rolled back; nothing is saved…'));
    try {
      const r = await send('/api/admin/import/check', { job_id: job.job_id, as_of: asOf.value || null });
      job = { ...job, log: r.log, ok: r.ok, warning: r.warning || null, outputs: r.outputs, state: r.ok ? 'CHECKED' : 'CHECK_FAILED' };
    } catch (e) { job = { ...job, log: e.message, ok: false, state: 'CHECK_FAILED' }; }
    drawStep();
  }

  function drawStep() {
    if (!job) { stepEl.replaceChildren(h('p', { class: 'muted' }, 'Upload a file to start. Nothing is loaded until you press “Load into database”.')); return; }
    const [tone, text] = STATUS[job.state] || STATUS.UPLOADED;
    stepEl.replaceChildren(
      h('div', { class: 'kv' }, kv('File', job.filename), kv('Size', bytes(job.size_bytes)), kv('Status', h('span', { class: 'badge ' + tone }, text))),
      job.warning ? h('p', { class: 'rec-note warn', role: 'alert' }, icon('warning--alt--filled'), 'The rehearsal says this load would be refused: ' + job.warning) : null,
      h('h3', { class: 'small-h' }, 'Converter report'), h('pre', { class: 'log mono' }, job.log || '(no output yet)'),
      job.ok ? h('div', { class: 'btn-row' }, h('button', { class: 'btn primary', type: 'button', onClick: confirmLoad }, icon('data--base'), 'Load into database…'), h('span', { class: 'hint' }, 'A safety backup is taken first.')) : null,
      job.state === 'CHECK_FAILED' ? h('p', { class: 'rec-note bad' }, icon('error--filled'), 'The file did not pass the check. Nothing was changed. Fix the file (or the date) and upload again.') : null);
  }
  const kv = (k, v) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)];

  function confirmLoad() {
    const force = h('input', { type: 'checkbox', id: 'im-force' });
    openModal({
      title: 'Load into the database', lead: 'The current data is backed up automatically, then this file is applied. Your manual edits made in the portal are kept.',
      body: h('div', null, h('div', { class: 'kv' }, kv('File', job.filename), kv('Kind', meta.kinds.find((k) => k.key === job.kind)?.label || job.kind)),
        h('label', { class: 'opt', for: 'im-force' }, force, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, 'Allow a large number of records to disappear (only if the file is meant to remove them)'))),
      actions: [{ label: 'Cancel' }, { label: 'Load now', primary: true, onClick: async () => {
        const r = await send('/api/admin/import/load', { job_id: job.job_id, as_of: asOf.value || null, force: force.checked });
        job = { ...job, log: r.log, ok: r.ok, state: r.ok ? 'LOADED' : 'LOAD_FAILED' };
        if (r.ok) { job.ok = false; toast('Loaded'); } else toast('The load did not complete; nothing was changed.', 'bad');
        drawStep(); load();
      } }],
    });
  }

  function history() {
    if (!meta.history.length) return h('div', { class: 'muted', style: { padding: '16px' } }, 'No imports yet.');
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['When', 'File', 'Kind', 'Size', 'By', 'Status', 'Safety backup'].map((t) => h('th', null, t)))),
      h('tbody', null, meta.history.map((r) => h('tr', null, h('td', { class: 'nowrap' }, when(r.at)), h('td', { class: 'wrap' }, r.filename), h('td', null, r.kind), h('td', null, bytes(r.size_bytes || 0)), h('td', null, r.uploaded_by),
        h('td', null, h('span', { class: 'badge ' + STATUS[r.status][0] }, STATUS[r.status][1])), h('td', { class: 'mono faint' }, r.safety_backup || ''))))));
  }

  load();
  return { destroy() { dead = true; }, onLive: () => {} };
}

