// Replace asset / Redeploy asset (administrators only). Both are step-by-step windows that end in a "this is exactly what will change" review.
// The server does every check again and runs the whole change in one transaction - see portal/app/lifecycle.py.
import { get, send } from '../core/api.js';
import { debounce, h, icon } from '../core/dom.js';
import { date } from '../core/format.js';
import { toast } from '../core/editor.js';
import { openModal } from '../ui/modal.js';

const show = (x) => (x == null || x === '' ? '—' : String(x));
const today = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };
let seq = 0;

/** Labelled control with an inline error line that the server's field messages are written into. */
function field(label, control, { hint, required } = {}) {
  const id = 'lc' + (++seq);
  control.id = id;
  const err = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const row = h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label, required ? h('span', { class: 'req', title: 'Required' }, ' *') : null), control, hint ? h('div', { class: 'muted small' }, hint) : null, err);
  control.addEventListener('input', () => { err.hidden = true; row.classList.remove('bad'); });
  return { row, control, setError(msg) { err.replaceChildren(...(msg ? [icon('error--filled'), msg] : [])); err.hidden = !msg; row.classList.toggle('bad', !!msg); } };
}

function tick(text, checked = false) {
  const input = h('input', { type: 'checkbox' }); input.checked = checked;
  const label = h('label', { class: 'ropt' }, input, icon('checkbox', 'glyph off'), icon('checkbox--checked', 'glyph on'), h('span', null, text));
  return { label, input };
}

const mono = (t) => h('span', { class: 'mono' }, show(t));

function facts(rows) {
  return h('div', { class: 'kv compact' }, rows.flatMap(([k, v]) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v == null || v === '' ? h('span', { class: 'faint' }, '—') : v)]));
}

function reviewTable(rows) {
  return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Field', 'Before', 'After'].map((t) => h('th', null, t)))),
    h('tbody', null, rows.map(([f, b, a, keep]) => h('tr', null, h('td', null, f), keep ? h('td', { class: 'wrap', colspan: '2' }, h('strong', null, a), ' — unchanged') : h('td', { class: 'wrap faint' }, show(b)), keep ? null : h('td', { class: 'wrap' }, h('strong', null, show(a))))))));
}

function spreadErrors(e, fields, m) {
  let used = false;
  for (const [k, msg] of Object.entries(e.fields || {})) if (fields[k]) { fields[k].setError(msg); used = true; }
  if (!used || !e.fields) m.error(e.message || 'Something went wrong.');
  else m.clearError();
}

function shell({ title, lead }) {
  const body = h('div', { class: 'lc-body' });
  const m = openModal({ title, lead, wide: true, body, actions: [] });
  return { m, body };
}

const bar = (...btns) => h('div', { class: 'modal-actions', style: { margin: '16px -16px -12px' } }, btns);

// ---------------------------------------------------------------------------------------------------------------- replace
/** From the page of the machine going out: choose the machine that replaces it, then confirm. onDone(detail) gets the live record. */
export async function replaceDialog({ asset, onDone }) {
  const key = asset.asset_key;
  let pre;
  try { pre = await get('/api/lifecycle/preflight', { key }); } catch (e) { toast(e.message, 'bad'); return; }
  const { m, body } = shell({ title: `Replace ${key}`, lead: 'A new machine takes over this machine\'s name. This machine is retired as a separate record that keeps its own history.' });
  const st = { pick: null, f: {} };

  // ------------------------------------------------ step 1: pick the machine that comes in
  function step1() {
    m.el.querySelector('#modal-title').textContent = `Replace ${key} · 1 of 3`;
    const open = pre.open_calls + pre.open_rma;
    const checks = h('ul', { class: 'lc-checks' },
      h('li', null, icon(open ? 'warning--filled' : 'checkmark--filled', open ? 'bad' : 'ok'), open ? ` Still has ${pre.open_calls} open call(s) and ${pre.open_rma} OEM RMA case(s) not yet returned. Close or complete them first.` : ' No open calls or OEM RMA cases.'),
      h('li', null, icon('checkmark--filled', 'ok'), ` ${pre.history['change log']} change-log, ${pre.history.snapshots} snapshot and ${pre.history['PM snapshots']} PM-snapshot rows will stay with this machine${pre.components ? `, with its ${pre.components} component line(s)` : ''}.`));
    const results = h('div', { class: 'lc-results', role: 'radiogroup', 'aria-label': 'Replacement machine' });
    const search = h('input', { type: 'text', placeholder: 'Search by asset key, hostname, serial number, ONGC ID or model', autocomplete: 'off' });
    const sf = field('Which machine replaces it?', search, { hint: 'It must already be in the register as its own asset (for example the new laptop that was just added).' });
    const next = h('button', { class: 'btn primary', type: 'button', disabled: true, onClick: step2 }, 'Continue');
    const pickRow = (c) => {
      const r = h('input', { type: 'radio', name: 'lc-pick', value: c.asset_key });
      r.addEventListener('change', () => { st.pick = c; next.disabled = !!open; });
      return h('label', { class: 'ropt lc-cand' }, r, icon('radio-button', 'glyph off'), icon('radio-button--checked', 'glyph on'),
        h('span', null, h('strong', { class: 'mono' }, c.asset_key), ' ', [c.make, c.model].filter(Boolean).join(' '), h('span', { class: 'muted small' }, ` · serial ${show(c.serial_no)} · ONGC ${show(c.ongc_asset_id)}${c.user_name ? ' · ' + c.user_name : ''}`)));
    };
    const run = debounce(async () => {
      const q = search.value.trim();
      if (q.length < 2) { results.replaceChildren(); return; }
      try {
        const { rows } = await get('/api/lifecycle/candidates', { q, exclude: key });
        results.replaceChildren(...(rows.length ? rows.map(pickRow) : [h('p', { class: 'muted' }, 'No matching current asset. Add the new machine to the register first.')]));
        if (st.pick) { const r = results.querySelector(`input[value="${CSS.escape(st.pick.asset_key)}"]`); if (r) r.checked = true; }
      } catch (e) { m.error(e.message); }
    }, 250);
    search.addEventListener('input', run);
    body.replaceChildren(
      h('h3', null, 'This machine (going out)'),
      facts([['Asset (CI)', mono(key)], ['Make / model', [asset.make, asset.model].filter(Boolean).join(' ')], ['Serial no.', mono(asset.serial_no)], ['ONGC asset ID', mono(asset.ongc_asset_id)],
        ['User', asset.user_name], ['Location', [asset.location_code, asset.floor_area, asset.room].filter(Boolean).join(', ')]]),
      checks, sf.row, results, bar(h('button', { class: 'btn', type: 'button', onClick: () => m.close() }, 'Cancel'), next));
    search.focus();
  }

  // ------------------------------------------------ step 2: the details
  function step2() {
    m.el.querySelector('#modal-title').textContent = `Replace ${key} · 2 of 3`;
    const c = st.pick;
    const reason = h('select', null, pre.reasons.map((r) => h('option', { value: r.value }, r.label)));
    const when = h('input', { type: 'date', value: st.f.date || today() });
    const disposal = h('input', { type: 'text', maxlength: '80', placeholder: 'Condemnation note or disposal reference (can be added later)', value: st.f.disposal || '' });
    const serial = h('input', { type: 'text', maxlength: '60', value: st.f.serial ?? c.serial_no ?? '', placeholder: 'Printed on the machine\'s service tag' });
    const host = h('input', { type: 'text', maxlength: '60', value: st.f.host || '', placeholder: key });
    const wiped = tick('The old machine\'s data was wiped, or its disks were handed to the data owner', st.f.wiped);
    const cu = tick(`User: ${show(asset.user_name)}${asset.cpf_no ? ' (CPF ' + asset.cpf_no + ')' : ''}`, st.f.cu ?? true), cl = tick(`Location: ${[asset.location_code, asset.floor_area, asset.room].filter(Boolean).join(', ') || '—'}`, st.f.cl ?? true), ce = tick(`Support engineer: ${show(asset.engineer_name)}`, st.f.ce ?? true);
    reason.value = st.f.reason || reason.value;
    const F = { reason: field('Why is the old machine being retired?', reason, { required: true }), date: field('Replacement date', when, { required: true }), disposal_ref: field('Disposal reference', disposal),
      serial_no: field('New machine\'s serial number', serial, { required: true, hint: c.serial_no ? 'Taken from the register - change it only if it is wrong.' : 'Missing in the register. It is the machine\'s real identity, so it is required.' }),
      hostname: field('New machine\'s hostname', host, { hint: `Leave blank to use ${key}.` }) };
    st.F = F;
    const go = h('button', { class: 'btn primary', type: 'button', onClick: () => {
      if (!serial.value.trim()) { F.serial_no.setError('Enter the new machine\'s serial number.'); serial.focus(); return; }
      Object.assign(st.f, { reason: reason.value, date: when.value, disposal: disposal.value.trim(), serial: serial.value.trim(), host: host.value.trim(), wiped: wiped.input.checked, cu: cu.input.checked, cl: cl.input.checked, ce: ce.input.checked });
      step3();
    } }, 'Review changes');
    body.replaceChildren(
      h('div', { class: 'rec-note info' }, icon('information--filled'), h('div', null, h('strong', { class: 'mono' }, c.asset_key), ` (${[c.make, c.model].filter(Boolean).join(' ')}) will become `, h('strong', { class: 'mono' }, key), '.')),
      F.reason.row, F.date.row, F.disposal_ref.row, h('div', { class: 'frow' }, wiped.label), F.serial_no.row, F.hostname.row,
      h('h3', null, 'Carry the seat over to the new machine'), h('div', { class: 'frow' }, cu.label, cl.label, ce.label),
      h('div', { class: 'rec-note warn' }, icon('warning--filled'), 'Not carried over, on purpose: this machine\'s cover, its PM status, its specifications, and its calls, RMAs and PM history. They belong to the machine going out.'),
      bar(h('button', { class: 'btn', type: 'button', onClick: step1 }, 'Back'), go));
    serial.focus();
  }

  // ------------------------------------------------ step 3: review and confirm
  function step3() {
    m.el.querySelector('#modal-title').textContent = `Replace ${key} · 3 of 3`;
    const c = st.pick, f = st.f, ymd = f.date.replace(/-/g, '');
    const retired = `${key}-RET-${ymd}`;
    const oldRows = [['Asset key', key, retired], ['CI number', key, retired], ['Status', asset.asset_status, 'REPLACED (retired)'], ['Replaced by', null, key], ['Date / reason', null, `${f.date} / ${pre.reasons.find((r) => r.value === f.reason)?.label}`],
      ['Disposal reference', null, f.disposal || 'to be added later'], ['Data wiped', null, f.wiped ? 'Yes' : 'Not recorded'], ['Serial no.', asset.serial_no, asset.serial_no, true], ['ONGC asset ID', asset.ongc_asset_id, asset.ongc_asset_id, true]];
    const newRows = [['Asset key', c.asset_key, key], ['CI number', c.asset_key, key], ['Hostname', c.hostname, f.host || key], ['Serial no.', c.serial_no, f.serial]];
    if (f.cu) newRows.push(['User', null, `${show(asset.user_name)}${asset.cpf_no ? ' (CPF ' + asset.cpf_no + ')' : ''}`]);
    if (f.cl) newRows.push(['Location', null, [asset.location_code, asset.floor_area, asset.room].filter(Boolean).join(', ')]);
    if (f.ce) newRows.push(['Support engineer', null, asset.engineer_name]);
    newRows.push(['PM', null, 'starts clean (not copied)'], ['Cover', null, 'keeps its own', true], ['ONGC asset ID', c.ongc_asset_id, c.ongc_asset_id, true]);
    const sure = tick('I have taken a backup and confirm this replacement.');
    const go = h('button', { class: 'btn danger', type: 'button', disabled: true }, icon('restart'), 'Replace asset');
    sure.input.addEventListener('change', () => { go.disabled = !sure.input.checked; });
    go.addEventListener('click', async () => {
      go.disabled = true; m.clearError();
      try {
        const r = await send('/api/lifecycle/replace', { key, replacement_key: c.asset_key, reason: f.reason, date: f.date, disposal_ref: f.disposal, data_wiped: f.wiped, serial_no: f.serial, hostname: f.host,
          carry: { user: f.cu, location: f.cl, engineer: f.ce } });
        m.close(); toast(`${r.retired_key} retired; ${r.live_key} is now the new machine`); onDone?.(r.detail);
      } catch (e) { go.disabled = false; if (e.fields && (e.fields.serial_no || e.fields.reason || e.fields.date)) { m.error(e.message); step2(); spreadErrors(e, st.F, m); } else m.error(e.message); }
    });
    body.replaceChildren(h('p', { class: 'muted' }, 'All of this happens in one step. If anything fails, nothing changes.'),
      h('h3', null, 'The old machine becomes a retired record'), reviewTable(oldRows),
      h('h3', null, h('span', null, 'The new machine becomes the live '), mono(key)), reviewTable(newRows),
      h('div', { class: 'rec-note info' }, icon('information--filled'), 'Its history (change log, snapshots, PM snapshots, calls, RMAs) moves to the retired key; the new machine keeps only its own. After this, fix the spreadsheet: the new machine\'s row takes the name ' + key + ', and the old machine\'s row is removed. If the sheet is still out of date, the next import will stop and tell you which rows to fix.'),
      h('div', { class: 'frow', style: { marginTop: '12px' } }, sure.label), bar(h('button', { class: 'btn', type: 'button', onClick: step2 }, 'Back'), go));
  }

  step1();
}

// ---------------------------------------------------------------------------------------------------------------- redeploy
/** From the page of a retired machine: put the same physical machine back into service under a new name. onDone(detail, newKey). */
export async function redeployDialog({ asset, engineers = [], onDone }) {
  const key = asset.asset_key;
  let pre;
  try { pre = await get('/api/lifecycle/preflight', { key }); } catch (e) { toast(e.message, 'bad'); return; }
  const needsApproval = !asset.retired_reason || pre.reasons.find((r) => r.value === asset.retired_reason)?.needs_approval;
  const { m, body } = shell({ title: `Redeploy ${key}`, lead: 'The same physical machine goes back into service under a new name. It stays one record, with its history.' });
  const f = {};

  function step1() {
    m.el.querySelector('#modal-title').textContent = `Redeploy ${key} · 1 of 2`;
    const nk = h('input', { type: 'text', maxlength: '40', value: f.key || '', placeholder: 'e.g. ANKA-TRNWS014', autocapitalize: 'characters' });
    const host = h('input', { type: 'text', maxlength: '60', value: f.host || '', placeholder: 'Same as the new key unless it differs' });
    const cpf = h('input', { type: 'text', inputmode: 'numeric', maxlength: '8', value: f.cpf || '', placeholder: 'Optional - new user CPF' });
    const loc = h('input', { type: 'text', maxlength: '40', value: f.loc || '', placeholder: 'Location code' });
    const room = h('input', { type: 'text', maxlength: '60', value: f.room || '', placeholder: 'Floor / room' });
    const eng = h('input', { type: 'text', list: 'lc-engineers', value: f.eng || '', placeholder: 'Support engineer' });
    const dl = h('datalist', { id: 'lc-engineers' }, engineers.map((e) => h('option', { value: e })));
    const purpose = h('select', null, ['Spare / pool', 'Training room', 'Lab / test bench', 'Another section', 'Other'].map((p) => h('option', null, p)));
    purpose.value = f.purpose || purpose.value;
    const approval = h('input', { type: 'text', maxlength: '80', value: f.approval || '', placeholder: needsApproval ? 'Required - the manager\'s note or mail reference' : 'Optional' });
    const when = h('input', { type: 'date', value: f.date || today() });
    const wiped = tick('Old data was wiped (required)', f.wiped), pm = tick('Restart the PM schedule from the redeploy date', f.pm ?? true), cover = tick(`Keep the existing cover (${show(asset.cover_type)} to ${asset.cover_expiry_date ? date(asset.cover_expiry_date) : '—'})`, f.cover ?? true);
    const F = { new_key: field('New asset key / CI number', nk, { required: true, hint: 'The hostname defaults to this.' }), hostname: field('New hostname', host), cpf_no: field('New user (CPF)', cpf),
      location_code: field('New location', loc), room: field('Floor / room', room), engineer_name: field('Support engineer', eng), approval_ref: field('Manager\'s approval reference', approval, { required: needsApproval }),
      date: field('Redeploy date', when), data_wiped: { row: h('div', { class: 'frow' }, wiped.label), setError() {} } };
    const go = h('button', { class: 'btn primary', type: 'button', onClick: () => {
      if (!nk.value.trim()) { F.new_key.setError('Enter the new asset key.'); nk.focus(); return; }
      if (needsApproval && !approval.value.trim()) { F.approval_ref.setError('This machine was condemned or archived. Enter the manager\'s approval reference.'); approval.focus(); return; }
      if (!wiped.input.checked) { m.error('Confirm that the old data was wiped before the machine goes back into service.'); return; }
      m.clearError();
      Object.assign(f, { key: nk.value.trim().toUpperCase(), host: host.value.trim(), cpf: cpf.value.trim(), loc: loc.value.trim(), room: room.value.trim(), eng: eng.value.trim(), purpose: purpose.value, approval: approval.value.trim(), date: when.value, wiped: true, pm: pm.input.checked, cover: cover.input.checked });
      step2();
    } }, 'Review changes');
    st1 = F;
    body.replaceChildren(
      h('div', { class: 'lc-identity' }, h('div', { class: 'muted small' }, 'Physical identity - locked, it will not change'),
        facts([['Serial no.', h('span', null, mono(asset.serial_no), ' ', icon('locked'))], ['ONGC asset ID', h('span', null, mono(asset.ongc_asset_id), ' ', icon('locked'))], ['Census no.', h('span', null, mono(asset.ongc_census_no), ' ', icon('locked'))],
          ['Retired', asset.retired_on ? `${date(asset.retired_on)} (${pre.reasons.find((r) => r.value === asset.retired_reason)?.label || 'archived'})` : 'archived']])),
      F.new_key.row, F.hostname.row, h('h3', null, 'Where it goes'), F.cpf_no.row, F.location_code.row, F.room.row, F.engineer_name.row, dl,
      h('div', { class: 'frow' }, h('label', { class: 'flabel' }, 'Purpose'), purpose), F.approval_ref.row, F.date.row,
      h('h3', null, 'Before it goes live'), h('div', { class: 'frow' }, wiped.label, pm.label, cover.label),
      bar(h('button', { class: 'btn', type: 'button', onClick: () => m.close() }, 'Cancel'), go));
    nk.focus();
  }
  let st1;

  function step2() {
    m.el.querySelector('#modal-title').textContent = `Redeploy ${key} · 2 of 2`;
    const rows = [['Asset key', key, f.key], ['CI number', asset.ci_no, f.key], ['Hostname', asset.hostname, f.host || f.key], ['Status', 'REPLACED (retired)', 'IN_USE'], ['User', asset.user_name, f.cpf ? `CPF ${f.cpf}` : 'none (unassigned)'],
      ['Location', [asset.location_code, asset.room].filter(Boolean).join(', '), [f.loc, f.room].filter(Boolean).join(', ') || 'none'], ['Support engineer', asset.engineer_name, f.eng || 'none'],
      ['Purpose', null, f.purpose], ['PM', asset.pm_status, f.pm ? 'starts clean from ' + f.date : 'unchanged'], ['Cover', asset.cover_type, f.cover ? 'unchanged' : 'removed'],
      ['Serial no.', asset.serial_no, asset.serial_no, true], ['ONGC asset ID', asset.ongc_asset_id, asset.ongc_asset_id, true], ['Census no.', asset.ongc_census_no, asset.ongc_census_no, true]];
    const sure = tick('I have taken a backup and confirm this redeployment.');
    const go = h('button', { class: 'btn primary', type: 'button', disabled: true }, icon('play'), 'Redeploy asset');
    sure.input.addEventListener('change', () => { go.disabled = !sure.input.checked; });
    go.addEventListener('click', async () => {
      go.disabled = true; m.clearError();
      try {
        const r = await send('/api/lifecycle/redeploy', { key, new_key: f.key, hostname: f.host, cpf_no: f.cpf, location_code: f.loc, room: f.room, engineer_name: f.eng, purpose: f.purpose, approval_ref: f.approval,
          date: f.date, data_wiped: true, restart_pm: f.pm, keep_cover: f.cover });
        m.close(); toast(`${r.former_key} is back in service as ${r.live_key}`); onDone?.(r.detail, r.live_key);
      } catch (e) { go.disabled = false; if (e.fields && Object.keys(e.fields).length) { step1(); spreadErrors(e, st1, m); } else m.error(e.message); }
    });
    body.replaceChildren(h('p', { class: 'muted' }, 'One step; if anything fails, nothing changes.'), reviewTable(rows),
      h('div', { class: 'rec-note info' }, icon('information--filled'), `The machine's history comes with it, and ${key} is kept as a former name so it can still be found. Put the new name on this machine's row in the spreadsheet, keeping its serial number and ONGC asset ID.`),
      h('div', { class: 'frow', style: { marginTop: '12px' } }, sure.label), bar(h('button', { class: 'btn', type: 'button', onClick: step1 }, 'Back'), go));
  }

  step1();
}
