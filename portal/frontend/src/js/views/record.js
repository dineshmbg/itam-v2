// The record drawer: read view, edit form, archive / restore, manual-override markers and history. Also the "New record" window.
import { send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { date, label as fieldLabel } from '../core/format.js';
import { getSchema, toast } from '../core/editor.js';
import { isAdmin, user as sessionUser } from '../core/session.js';
import { buildForm } from '../ui/form.js';
import { openModal } from '../ui/modal.js';
import { renderDetail } from './detail.js';
import { recordPmDialog } from './pm-record.js';

const ACTIONS = { UPDATE: ['edit', 'Changed'], CREATE: ['add', 'Created'], ARCHIVE: ['archive', 'Archived'], RESTORE: ['renew', 'Restored'], RESET: ['undo', 'Override removed'], CASCADE: ['link', 'Follow-on update'], EVENT: ['user', 'Event'], VERIFY: ['checkmark--outline', 'Physical check'] };

const short = (v) => (v == null || v === '' ? '—' : String(v));

/** Drive an open drawer for one record. payload: response of GET /api/registers/<name>/<id>. */
export async function mountRecord({ drawer, name, payload, schema, onChange }) {
  schema ||= await getSchema();
  const spec = schema.datasets[name] || { fields: [], readonly: true, defaults: {} };
  const admin = isAdmin();
  let p = payload;
  let form = null;

  const fieldSpec = (k) => spec.fields.find((f) => f.key === k);
  const canEditAny = () => spec.fields.some((f) => !f.readonly && !f.create_only);
  // Engineer records: an admin can edit any of them; anyone else only their own (the one matching their own login).
  const isOwnEngineerRecord = () => admin || sessionUser()?.engineer_key === p.id;
  const canArchiveThis = name !== 'engineers' && (admin || name !== 'assets');

  function show() {
    form = null;
    const bar = h('div', { class: 'rec-bar' });
    if (p.archived) {
      bar.append(h('div', { class: 'rec-note warn' }, icon('archive'), 'This record is archived and hidden from the registers.'));
      if (!spec.readonly && canArchiveThis) bar.append(h('button', { class: 'btn', type: 'button', onClick: restore }, icon('renew'), 'Restore'));
    } else if (!spec.readonly) {
      if (canEditAny() && (name !== 'engineers' || isOwnEngineerRecord())) bar.append(h('button', { class: 'btn primary', type: 'button', onClick: edit }, icon('edit'), 'Edit'));
      if (name === 'assets' && p.row.record_level === 'ASSET' && ['PENDING', 'DONE', 'DONE_OUTSIDE_QUARTER'].includes(p.row.pm_status)) {
        bar.append(h('button', { class: 'btn', type: 'button', onClick: () => recordPmDialog({ keys: [p.id], defaults: { engineer: p.row.pm_done_by || p.row.engineer_name, signed: p.row.pm_signed_by }, onDone: () => reload().then(() => onChange?.()) }) }, icon('checkmark'), 'Record PM'));
      }
      if (canArchiveThis) bar.append(h('button', { class: 'btn', type: 'button', onClick: archive }, icon('archive'), 'Archive'));
      if (name === 'calls' && p.row.call_status === 'OPEN' && !(p.related.inward || []).length) {
        bar.append(h('button', { class: 'btn primary', type: 'button', style: { marginLeft: 'auto' },
          onClick: () => newRecord({ name: 'inward', label: 'Inward', prefill: { sr_id: p.id, asset_key: p.row.asset_key || '' }, onCreated: () => reload().then(() => onChange?.()) }) }, icon('add'), 'Inward'));
      }
      if (name === 'calls' && p.row.call_status === 'CLOSED' && !(p.related.outward || []).length) {
        bar.append(h('button', { class: 'btn primary', type: 'button', style: { marginLeft: 'auto' },
          onClick: () => newRecord({ name: 'outward', label: 'Outward', prefill: { sr_id: p.id, asset_key: p.row.asset_key || '' }, onCreated: () => reload().then(() => onChange?.()) }) }, icon('add'), 'Outward'));
      }
    }
    if (name === 'assets' && !p.archived && p.row.record_level === 'ASSET') {
      const last = (p.related.verifications || [])[0];
      if (!spec.readonly && (admin || sessionUser()?.engineer_key === p.row.engineer_name)) {
        bar.append(h('button', { class: 'btn', type: 'button', title: last ? `Last checked ${date(last.verified_on)} by ${last.verified_by}` : 'Never physically checked', onClick: verifyDialog }, icon('checkmark--outline'), 'Verify'));
      }
      bar.append(h('button', { class: 'btn', type: 'button', title: 'Print a QR label for this asset', onClick: labelDialog }, icon('qr-code'), 'Label'));
    }
    if (p.created_in_portal) bar.append(h('span', { class: 'badge info' }, icon('add'), 'Added in the portal'));
    const parts = [bar, ...renderDetail(name, p, { overrides: p.overrides, onReset: resetField, canEdit: (k) => { const f = fieldSpec(k); return f && !f.readonly && !f.create_only; } })];
    const hist = p.related.audit || [];
    if (hist.length) parts.push(history(hist));
    drawer.body.replaceChildren(...parts);
  }

  function history(rows) {
    return h('div', { class: 'dsec' }, h('h3', null, `Edit history (${rows.length})`), h('ol', { class: 'timeline' }, rows.map((r) => {
      const [ic, verb] = ACTIONS[r.action] || ['edit', r.action];
      const ch = r.changes || {};
      const parts = Object.entries(ch).filter(([k]) => k !== 'date').map(([k, v]) => (v && typeof v === 'object' && 'new' in v ? `${fieldLabel(k)}: ${short(v.old)} → ${short(v.new)}` : null)).filter(Boolean);
      return h('li', null, h('span', { class: 'tl-ico' }, icon(ic)), h('div', null,
        h('div', null, h('strong', null, verb), ' by ', h('span', null, r.editor), h('span', { class: 'faint' }, ' · ' + when(r.at))),
        parts.length ? h('ul', { class: 'tl-changes' }, parts.map((t) => h('li', null, t))) : null,
        r.reason ? h('div', { class: 'muted' }, r.reason) : null));
    })));
  }

  function edit() {
    form = buildForm({ fields: spec.fields.map((f) => ({ ...f, create_only: f.create_only })).filter((f) => !f.readonly), values: p.row, engineers: schema.engineers, onInput: () => { save.disabled = !Object.keys(form.changes()).length; } });
    const banner = h('div', { class: 'rec-note bad', role: 'alert', hidden: true });
    const reason = h('input', { type: 'text', id: 'rec-reason', maxlength: '200', placeholder: 'Optional: why is this being changed?' });
    const save = h('button', { class: 'btn primary', type: 'button', disabled: true }, icon('save'), 'Save changes');
    const cancel = h('button', { class: 'btn', type: 'button', onClick: show }, 'Cancel');
    if (name === 'assets' && !p.row.install_date) {
      // Installed On was blank - offer today's date as a real pending change (must still click Save changes), never touched if it already had one.
      form.setValue('install_date', todayISO());
      save.disabled = false;
    }
    save.addEventListener('click', async () => {
      const changes = form.changes();
      if (!Object.keys(changes).length) return;
      banner.hidden = true; save.disabled = true;
      const expected = Object.fromEntries(Object.keys(changes).map((k) => [k, p.row[k] ?? null]));
      try {
        const r = await send(`/api/edit/${name}/update`, { key: p.id, changes, expected, reason: reason.value.trim() || null });
        p = r.detail;
        toast(r.changed?.length ? 'Saved' : 'No changes');
        show(); onChange?.();
      } catch (e) {
        save.disabled = false;
        if (e.status === 409 && Object.keys(e.current || {}).length) {
          banner.replaceChildren(icon('warning--alt--filled'), h('div', null, h('strong', null, 'Someone changed this record after you opened it. '),
            Object.entries(e.current).map(([k, v]) => h('div', null, `${fieldLabel(k)} is now “${short(v.current)}”.`)),
            h('button', { class: 'link-btn', type: 'button', onClick: reload }, 'Discard my edits and load the latest')));
        } else {
          banner.replaceChildren(icon('error--filled'), h('div', null, e.message));
          form.setErrors(e.fields);
        }
        banner.hidden = false; banner.scrollIntoView({ block: 'nearest' });
      }
    });
    drawer.body.replaceChildren(h('div', { class: 'rec-note info' }, icon('information--filled'), 'Status, ageing and flags update automatically when you save. Text is stored in capitals.'), banner, form.el,
      h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'rec-reason' }, 'Reason'), reason), h('div', { class: 'form-actions' }, cancel, save));
    form.focusFirst();
  }

  async function reload() {
    const { get } = await import('../core/api.js');
    p = await get(`/api/registers/${name}/${encodeURIComponent(p.id)}`);
    show();
  }

  function archive() {
    const reason = h('textarea', { rows: '3', id: 'ar-reason', maxlength: '200', placeholder: 'Why is this record being archived?' });
    openModal({
      title: `Archive ${p.id}`, lead: 'The record disappears from the registers and dashboards. Nothing is deleted - it can be restored from the archive.',
      body: h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'ar-reason' }, 'Reason (required)'), reason),
      actions: [{ label: 'Cancel' }, { label: 'Archive', danger: true, onClick: async () => {
        await send(`/api/edit/${name}/archive`, { key: p.id, reason: reason.value });
        toast(`${p.id} archived`);
        onChange?.(); drawer.close();
      } }],
    });
  }

  /** Physical check (stocktake): record that the asset was, or was not, found where the register says. */
  function verifyDialog() {
    const last = (p.related.verifications || [])[0];
    const radios = [['FOUND', 'Found where the register says'], ['NOT_FOUND', 'Not found']].map(([v, t], i) => {
      const r = h('input', { type: 'radio', name: 'vf-result', value: v }); r.checked = i === 0;
      return h('label', { class: 'ropt' }, r, icon('radio-button', 'glyph off'), icon('radio-button--checked', 'glyph on'), h('span', null, t));
    });
    const group = h('div', { class: 'rgroup', role: 'radiogroup', 'aria-label': 'Result' }, radios);
    const note = h('textarea', { id: 'vf-note', rows: '3', maxlength: '300', placeholder: 'Optional - e.g. moved to room 12, screen cracked' });
    openModal({
      title: `Verify ${p.id}`, lead: last ? `Last checked ${date(last.verified_on)} by ${last.verified_by} (${last.result === 'FOUND' ? 'found' : 'not found'}).` : 'This asset has never been physically checked.',
      body: h('div', null, h('div', { class: 'frow' }, h('span', { class: 'flabel' }, 'Result'), group), h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'vf-note' }, 'Note'), note)),
      actions: [{ label: 'Cancel' }, { label: 'Record check', primary: true, onClick: async () => {
        const r = await send('/api/edit/assets/verify', { key: p.id, result: group.querySelector('input:checked').value, note: note.value });
        p = r.detail; toast('Check recorded'); show(); onChange?.();
      } }],
    });
  }

  /** QR label: scanning it opens this asset's record. Printed through a temporary copy that is the only thing the print stylesheet shows. */
  async function labelDialog() {
    let d;
    try { const { get } = await import('../core/api.js'); d = await get(`/api/assets/${encodeURIComponent(p.id)}/qr`); } catch (e) { toast(e.message, 'bad'); return; }
    const label = () => {
      const box = h('div', { class: 'asset-label' }, h('div', { class: 'al-qr' }), h('div', { class: 'al-text' }, h('div', { class: 'al-key' }, d.key), ...d.lines.filter(Boolean).map((t) => h('div', { class: 'al-line' }, t))));
      box.querySelector('.al-qr').innerHTML = d.svg;      // generated by the server from this portal's own address - never from user input
      return box;
    };
    openModal({
      title: `Label for ${d.key}`, lead: 'Stick this on the asset. Scanning the code opens its record in this portal (sign-in required).',
      body: h('div', null, label(), h('p', { class: 'muted small' }, 'The code points to ', h('span', { class: 'mono' }, d.url))),
      actions: [{ label: 'Close' }, { label: 'Print', primary: true, icon: 'printer', keepOpen: true, onClick: () => {
        const sheet = h('div', { class: 'print-only' }, label());
        document.body.append(sheet);
        window.print();
        setTimeout(() => sheet.remove(), 500);
        return false;
      } }],
    });
  }

  async function restore() {
    try {
      const r = await send(`/api/edit/${name}/restore`, { key: p.id });
      p = r.detail; toast('Restored'); show(); onChange?.();
    } catch (e) { toast(e.message, 'bad'); }
  }

  async function resetField(field) {
    try {
      const r = await send(`/api/edit/${name}/reset`, { key: p.id, fields: [field] });
      p = r.detail; toast('Manual override removed'); show(); onChange?.();
    } catch (e) { toast(e.message, 'bad'); }
  }

  show();
  return { refresh(next) { if (!form) { p = next; show(); } } };
}

function when(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : `${date(iso.slice(0, 10))} ${d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`;
}

// The field(s) on each register whose default is "today" - recomputed fresh every time the New-record window opens (see below),
// never taken from the cached schema: getSchema() fetches once and keeps that response for the rest of the browser session, so a
// portal tab left open across midnight would otherwise still show yesterday's date here.
const TODAY_DEFAULT_FIELDS = { assets: ['install_date'], calls: ['cipl_call_date'], inward: ['inward_date', 'received_date'], outward: ['outward_date', 'sent_date'] };
const todayISO = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };

/** "New record" window for a register. onCreated(id) is called after the record exists. `prefill`: extra starting values on
 *  top of the register's own defaults (e.g. the SR ID and asset when creating an inward/outward line from a call's detail page). */
export async function newRecord({ name, label, onCreated, prefill = {} }) {
  const schema = await getSchema();
  const spec = schema.datasets[name];
  if (!schema.can_create[name]) { toast('Only administrators can add records to this register.', 'bad'); return; }
  const baseValues = { ...spec.defaults, ...prefill };
  for (const f of TODAY_DEFAULT_FIELDS[name] || []) if (!(f in prefill)) baseValues[f] = todayISO();
  const form = buildForm({ fields: spec.fields, values: baseValues, engineers: schema.engineers, create: true });
  const idNote = spec.id_auto ? h('p', { class: 'muted small' }, 'The ID is assigned automatically when you save.') : null;
  openModal({
    title: `New ${label}`, wide: true, body: h('div', null, idNote, form.el),
    actions: [{ label: 'Cancel' }, { label: 'Add', primary: true, icon: 'add', keepOpen: true, onClick: async (m) => {
      const miss = form.missing();
      if (Object.keys(miss).length) { form.setErrors(miss); throw new Error('Fill in the required fields (marked *).'); }
      const values = form.changes();
      try {
        const r = await send(`/api/edit/${name}/create`, { values: { ...baseValues, ...values } });
        toast(`${r.id} added`);
        m.close(); onCreated?.(r.id);
      } catch (e) { form.setErrors(e.fields); throw e; }
    } }],
  });
  form.focusFirst();
}
