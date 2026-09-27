// Backup and restore. Automatic backups run on a schedule; manual backups on demand. Restore replaces the live data and needs a typed confirmation.
import { download, get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../core/editor.js';
import { openModal } from '../ui/modal.js';
import { errorBlock, pageHead, panel } from './common.js';
import { when } from './audit.js';

const KIND = { MANUAL: ['info', 'Manual'], AUTO: ['mute', 'Automatic'], PRE_RESTORE: ['warn', 'Before restore'], PRE_IMPORT: ['warn', 'Before import'] };
const bytes = (n) => (n < 1048576 ? `${Math.round((n || 0) / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);

export function mountBackup(root) {
  let dead = false, data = null;
  const holder = h('div', { class: 'page' });
  root.append(holder);
  const body = h('div', { class: 'grid' });
  holder.append(pageHead('Backup and restore', 'Copies of the whole database. Keep at least one on another disk or machine.', [h('button', { class: 'btn primary', type: 'button', onClick: backupNow }, icon('data--base'), 'Back up now')]), body);

  async function load() {
    try { data = await get('/api/admin/backups'); if (dead) return; draw(); } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, load)); }
  }

  function draw() {
    const a = data.auto;
    const en = h('input', { type: 'checkbox', id: 'bk-en' }); en.checked = a.enabled;
    const tm = h('input', { type: 'time', id: 'bk-time', value: a.time });
    const keep = h('input', { type: 'number', id: 'bk-keep', min: '1', max: '365', value: String(a.keep), inputmode: 'numeric' });
    const save = h('button', { class: 'btn', type: 'button', onClick: async () => {
      try { await send('/api/admin/backups/settings', { enabled: en.checked, time: tm.value, keep: Number(keep.value) }); toast('Schedule saved'); load(); } catch (e) { toast(e.message, 'bad'); }
    } }, icon('save'), 'Save schedule');
    body.replaceChildren(
      panel('Automatic backup', { cls: 'span-4' }, h('div', null,
        h('label', { class: 'opt', for: 'bk-en' }, en, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, 'Back up automatically every day')),
        h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'bk-time' }, 'Time of day'), tm), h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'bk-keep' }, 'Automatic copies to keep'), keep),
        h('p', { class: 'hint' }, `Saved in: ${data.folder}. Manual and safety copies are never deleted automatically.`), save)),
      panel('Backups', { cls: 'span-8', flush: true }, table()));
  }

  function table() {
    if (!data.backups.length) return h('div', { class: 'muted', style: { padding: '16px' } }, 'No backups yet.');
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['When', 'Type', 'File', 'Size', 'By', ''].map((t) => h('th', null, t)))),
      h('tbody', null, data.backups.map((b) => {
        const [tone, label] = KIND[b.kind] || ['mute', b.kind];
        return h('tr', null, h('td', { class: 'nowrap' }, when(b.at)), h('td', null, h('span', { class: 'badge ' + tone }, label)), h('td', { class: 'mono wrap' }, b.file, b.exists ? null : h('div', { class: 'faint' }, 'file no longer on disk')),
          h('td', null, bytes(b.size_bytes)), h('td', null, b.made_by || ''),
          h('td', { class: 'right nowrap' }, b.exists && b.ok ? [
            h('button', { class: 'btn ghost', type: 'button', title: 'Check that the file is intact', onClick: () => verify(b) }, icon('checkmark--outline'), 'Verify'),
            h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Download this backup', title: 'Download', onClick: () => save(b) }, icon('download')),
            h('button', { class: 'btn ghost', type: 'button', onClick: () => restore(b) }, icon('undo'), 'Restore…')] : null));
      }))));
  }

  async function backupNow() {
    const note = h('input', { id: 'bk-note', type: 'text', maxlength: '200', placeholder: 'Optional note' });
    openModal({ title: 'Back up now', body: h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'bk-note' }, 'Note'), note),
      actions: [{ label: 'Cancel' }, { label: 'Back up', primary: true, onClick: async () => { const r = await send('/api/admin/backups/create', { note: note.value }); toast(`Backup ready (${bytes(r.size_bytes)})`); load(); } }] });
  }
  async function verify(b) {
    try { const r = await send('/api/admin/backups/verify', { file: b.file }); toast(`Intact - ${r.tables} tables can be restored`); } catch (e) { toast(e.message, 'bad'); }
  }
  async function save(b) {
    try { await download('/api/admin/backups/download', { file: b.file }, b.file); } catch (e) { toast(e.message, 'bad'); }
  }

  function restore(b) {
    const c = h('input', { id: 'rs-c', type: 'text', autocomplete: 'off', placeholder: 'RESTORE' });
    openModal({
      title: 'Restore this backup', lead: 'This REPLACES all current data - assets, calls, people, users, settings - with the contents of the backup. A safety backup of today\'s data is taken first, and the restore is all-or-nothing.',
      body: h('div', null, h('div', { class: 'kv' }, kv('Backup', b.file), kv('Taken', when(b.at)), kv('Size', bytes(b.size_bytes))),
        h('p', { class: 'rec-note warn' }, icon('warning--alt--filled'), 'Everyone is signed out afterwards if the backup holds different accounts. Work done after this backup was taken will only be in the safety copy.'),
        h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'rs-c' }, 'Type RESTORE to confirm'), c)),
      actions: [{ label: 'Cancel' }, { label: 'Restore', danger: true, onClick: async () => {
        const r = await send('/api/admin/backups/restore', { file: b.file, confirm: c.value });
        toast(`Restored. Safety copy: ${r.safety_backup}`);
        setTimeout(() => location.reload(), 1500);
      } }],
    });
  }
  const kv = (k, v) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)];

  load();
  return { destroy() { dead = true; }, onLive: () => {} };
}
