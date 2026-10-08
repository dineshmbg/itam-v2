// One PM work order: the checklist to fill in, findings, the sign-off steps (engineer, verifier, owner) and the history.
import { get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../ui/toast.js';
import { date, person, when } from '../core/format.js';
import * as router from '../core/router.js';
import { isAdmin, user } from '../core/session.js';
import { entity } from '../ui/hovercard.js';
import { openModal } from '../ui/modal.js';
import { errorBlock, loading, pageHead, panel } from './common.js';
import { ACK, SEVERITY, closeFindingDialog, frow, linkCallDialog, ownerDialog, setSeverity, stateBadges, today, verifyDialog } from './pm-wo-common.js';

const EVENTS = { CREATED: 'Work order created', STARTED: 'Started', COMPLETED: 'Completed and signed', COMPLETED_BATCH: 'Completed (batch sheet)', VERIFIED: 'Technically verified', VERIFICATION_REJECTED: 'Sent back by the verifier',
  OWNER_ACKNOWLEDGED: 'Owner accepted', OWNER_DISPUTED: 'Owner disputed', OWNER_DEEMED: 'Deemed accepted', CLOSED: 'Closed', CANCELLED: 'Cancelled', DEFER_REQUESTED: 'Deferral requested', DEFER_APPROVED: 'Deferral approved',
  DEFER_DECLINED: 'Deferral declined', FINDING_SEVERITY: 'Finding severity changed', FINDING_CALL_LINKED: 'Call linked to a finding', FINDING_CLOSED: 'Finding closed' };

export function mountPmWo(root) {
  const id = Number(router.current().path[2]);
  let dead = false, d = null;
  const answers = new Map();      // seq -> {result, value, note} edited but not saved yet
  const body = h('div', { class: 'grid' });
  const sub = h('span', null, '');
  const head = h('div');
  root.append(h('div', { class: 'page', 'data-mod': 'pm' }, head, body));
  body.append(loading());

  const mine = () => isAdmin() || (user()?.engineer_key && d.engineer_name === user().engineer_key);
  const editable = () => d && ['OPEN', 'IN_PROGRESS'].includes(d.state) && mine();

  async function load() {
    try {
      d = await get('/api/pmwo/detail', { id });
      if (dead) return;
      answers.clear();
      draw();
    } catch (e) { if (!dead) body.replaceChildren(h('div', { class: 'span-12' }, errorBlock(e, load))); }
  }

  const cur = (r) => ({ ...r, ...(answers.get(r.seq) || {}) });
  const set = (seq, patch) => { answers.set(seq, { ...(answers.get(seq) || {}), ...Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined)) }); };
  const answered = (r) => { const v = cur(r); return v.kind === 'VALUE' ? v.result === 'NA' || (v.value != null && v.value !== '') : v.result != null; };

  function lineControl(r) {
    const v = cur(r);
    if (!editable()) {
      const t = v.result === 'PASS' ? ['Pass', 'ok'] : v.result === 'FAIL' ? ['Fail', 'bad'] : v.result === 'NA' ? ['N/A', 'mute'] : ['Not answered', 'mute'];
      return h('div', { class: 'wo-ans' }, v.kind === 'VALUE' && v.value != null ? h('span', { class: 'mono' }, `${Number(v.value)} ${v.unit || ''}`) : null, h('span', { class: 'badge ' + t[1] }, t[0]));
    }
    if (r.kind === 'VALUE') {
      const lim = r.min_value != null || r.max_value != null ? ` (${r.min_value ?? '…'} to ${r.max_value ?? '…'})` : '';
      const inp = h('input', { type: 'number', step: 'any', class: 'wo-val', value: v.value ?? '', 'aria-label': `${r.text}${lim}` });
      inp.addEventListener('change', () => { set(r.seq, { value: inp.value, result: inp.value === '' ? null : undefined }); redrawLine(r.seq); });
      const na = h('button', { type: 'button', class: 'wo-seg-b', 'aria-pressed': String(v.result === 'NA'), onClick: () => { set(r.seq, { result: v.result === 'NA' ? null : 'NA', value: '' }); redrawLine(r.seq); } }, 'N/A');
      return h('div', { class: 'wo-ans' }, inp, h('span', { class: 'faint' }, `${r.unit || ''}${lim}`), na);
    }
    const mk = (val, label, tone) => h('button', { type: 'button', class: 'wo-seg-b ' + tone, 'aria-pressed': String(v.result === val), onClick: () => { set(r.seq, { result: v.result === val ? null : val }); redrawLine(r.seq); } }, label);
    return h('div', { class: 'wo-seg', role: 'group', 'aria-label': r.text }, mk('PASS', 'Pass', 'p'), mk('FAIL', 'Fail', 'f'), mk('NA', 'N/A', 'n'));
  }

  const lines = new Map();
  function lineRow(r) {
    const v = cur(r);
    const needNote = editable() && (v.result === 'FAIL' || v.result === 'NA');
    const note = needNote ? h('input', { type: 'text', class: 'no-upper wo-note', maxlength: '300', value: v.note || '', placeholder: v.result === 'FAIL' ? 'What did you find? (required)' : 'Why not applicable? (required)', 'aria-label': 'Note' }) : (v.note ? h('div', { class: 'faint' }, v.note) : null);
    note?.addEventListener?.('input', () => set(r.seq, { note: note.value }));
    return h('div', { class: 'wo-line', 'data-seq': r.seq }, h('span', { class: 'no' }, r.seq),
      h('div', null, h('div', null, r.text, r.mandatory ? h('span', { class: 'badge info', style: { marginLeft: '6px' } }, 'Mandatory') : null, r.critical ? h('span', { class: 'badge bad', style: { marginLeft: '4px' } }, 'Critical') : null), note), lineControl(r));
  }
  function redrawLine(seq) {
    const r = d.results.find((x) => x.seq === seq);
    const old = lines.get(seq);
    const fresh = lineRow(r);
    old.replaceWith(fresh);
    lines.set(seq, fresh);
    const active = fresh.querySelector('input.wo-note'); if (active && !active.value) active.focus();
    progress();
  }
  const prog = h('span', null, '');
  function progress() {
    const mand = d.results.filter((r) => r.mandatory);
    const n = mand.filter(answered).length;
    prog.textContent = `${n} of ${mand.length} required lines answered`;
  }

  function payload() {
    return [...answers.entries()].map(([seq, a]) => {
      const r = d.results.find((x) => x.seq === seq);
      const v = cur(r);
      return { seq, result: v.result ?? null, value: v.value === '' ? null : v.value, note: v.note ?? null };
    });
  }

  async function saveProgress(quiet) {
    d = await send('/api/pmwo/save', { id, results: payload(), minutes: minutes.value, remarks: remarks.value });
    answers.clear();
    if (!quiet) toast('Progress saved');
  }
  const minutes = h('input', { id: 'wo-min', type: 'number', min: '0', max: '1440', placeholder: 'Minutes', style: { maxWidth: '120px' } });
  const remarks = h('input', { id: 'wo-rem', type: 'text', class: 'no-upper', maxlength: '300', placeholder: 'Remarks (optional)' });

  function completeDialog() {
    const dt = h('input', { id: 'wo-d', type: 'date', value: today(), max: today() });
    openModal({
      title: `Complete and sign ${d.wo_no}`,
      lead: `You are signing that this PM was carried out on ${d.asset_key} as recorded. The asset's PM date is updated now${d.verify_required ? '; a second person will verify it' : ''}${d.owner_kind === 'SECTION' ? `, and the section (${d.owner_dept || 'owner'}) will be asked to confirm` : ', and the owner will be asked to confirm'}.`,
      body: frow('wo-d', 'PM done on', dt),
      actions: [{ label: 'Cancel' }, { label: 'Complete and sign', primary: true, icon: 'checkmark', onClick: async () => {
        await saveProgress(true);
        d = await send('/api/pmwo/complete', { id, pm_date: dt.value });
        toast('Work order completed');
        draw();
      } }],
    });
  }

  function deferDialog() {
    const reason = h('textarea', { id: 'df-r', maxlength: '300', placeholder: 'Why can it not be done by the due date?' });
    const to = h('input', { id: 'df-d', type: 'date', min: String(d.due_date).slice(0, 10) });
    openModal({
      title: 'Request a deferral', lead: `Due ${date(d.due_date)}. An administrator must approve; the work counts as a deviation in reports.`,
      body: h('div', null, frow('df-r', 'Reason', reason), frow('df-d', 'New date (within 60 days of the due date)', to)),
      actions: [{ label: 'Cancel' }, { label: 'Send request', primary: true, onClick: async () => { d = await send('/api/pmwo/defer-request', { id, reason: reason.value, to: to.value }); toast('Deferral requested'); draw(); } }],
    });
  }

  async function decideDeferral(approve) {
    try { d = await send('/api/pmwo/defer-decide', { id, approve, note: '' }); toast(approve ? 'Deferral approved' : 'Deferral declined'); draw(); } catch (e) { toast(e.message, 'bad'); }
  }

  function cancelDialog() {
    const reason = h('textarea', { id: 'cn-r', maxlength: '300', placeholder: 'For example: asset retired or replaced' });
    openModal({ title: `Cancel ${d.wo_no}`, lead: 'The work order stays on record as cancelled. The asset keeps showing PM pending until PM is recorded some other way or the asset is retired.', body: frow('cn-r', 'Reason', reason),
      actions: [{ label: 'Keep it' }, { label: 'Cancel work order', danger: true, onClick: async () => { d = await send('/api/pmwo/cancel', { id, reason: reason.value }); toast('Cancelled'); draw(); } }] });
  }

  function draw() {
    const badges = stateBadges(d).map(([t, tone]) => h('span', { class: 'badge ' + tone, style: { marginLeft: '6px' } }, t));
    sub.replaceChildren(`${d.quarter_label} · `, entity('asset', d.asset_key, d.asset_key), ' · ', ...badges);
    head.replaceChildren(pageHead(d.wo_no, sub, [h('a', { class: 'btn', href: '#/pm/work' }, icon('arrow--left', 'sm'), 'All work orders')]));
    lines.clear();
    const rowsEl = d.results.map((r) => { const el = lineRow(r); lines.set(r.seq, el); return el; });
    const ed = editable();
    if (ed && d.results.length) progress(); else prog.textContent = '';
    minutes.value = d.minutes_spent ?? ''; remarks.value = d.remarks || '';
    minutes.disabled = remarks.disabled = !ed;

    const facts = h('div', { class: 'kv compact' },
      ...[['Asset', entity('asset', d.asset_key, d.asset_key)], ['Class', `${d.asset_class || ''} · criticality ${d.criticality}`], ['Make / model', `${d.make || ''} ${d.model || ''}`.trim() || '—'], ['Serial', d.serial_no || '—'],
        ['Location', d.location_code || '—'], ['Engineer', d.engineer_name ? person(d.engineer_name) : 'Unassigned'],
        ['Owner', d.owner_kind === 'SECTION' ? `Section: ${d.owner_dept || '—'}` : [d.owner_name ? person(d.owner_name) : '—', d.owner_dept ? ` · ${d.owner_dept}` : '']],
        ['Due', d.defer_status === 'APPROVED' ? `${date(d.defer_to)} (deferred from ${date(d.due_date)})` : date(d.due_date)],
        ['Cover', d.cover_status || '—']].map(([k, v]) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)]).flat());

    const checklist = panel('Checklist', { cls: 'span-8', hint: ed ? prog : (d.legacy ? 'recorded before work orders existed' : '') },
      d.legacy ? h('p', { class: 'muted' }, `PM was recorded for this asset on ${date(d.pm_date)}${d.done_by ? ` by ${person(d.done_by)}` : ''} before work orders existed, so there is no checklist.`)
        : !d.results.length ? h('p', { class: 'muted' }, 'No checklist is defined for this class of asset.') : h('div', { class: 'wo-lines' }, rowsEl),
      ed ? h('div', { class: 'wo-foot' }, h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'wo-min' }, 'Time spent'), minutes), h('div', { class: 'frow', style: { flex: 1 } }, h('label', { class: 'flabel', for: 'wo-rem' }, 'Remarks'), remarks),
        h('div', { class: 'btn-row tight' },
          h('button', { class: 'btn', type: 'button', onClick: async () => { try { await saveProgress(); draw(); } catch (e) { toast(e.message, 'bad'); } } }, icon('save'), 'Save progress'),
          h('button', { class: 'btn primary', type: 'button', onClick: completeDialog }, icon('checkmark'), 'Complete and sign…'),
          d.defer_status !== 'REQUESTED' ? h('button', { class: 'btn', type: 'button', onClick: deferDialog }, icon('calendar'), 'Request deferral…') : null)) : null);

    const signoff = h('div', { class: 'wo-steps' },
      step('1', 'Performed', d.completed_at ? [`${d.done_by ? person(d.done_by) : d.completed_by}`, h('div', { class: 'faint' }, `${d.legacy ? date(d.pm_date) : when(d.completed_at)}${d.minutes_spent ? ` · ${d.minutes_spent} min` : ''}`)] : 'Not yet', d.completed_at ? 'ok' : 'mute'),
      step('2', 'Technical verification', !d.verify_required ? [h('span', { class: 'faint' }, 'Not required for this work order')] : d.verified_by ? [person(d.verified_by), h('div', { class: 'faint' }, `${when(d.verified_at)}${d.verify_note ? ' · ' + d.verify_note : ''}`)] : 'Required - a different person from the one who did it', d.verified_by ? 'ok' : d.verify_required ? 'warn' : 'mute'),
      step('3', 'Owner acknowledgement', ownerText(), { ACKNOWLEDGED: 'ok', DEEMED: 'warn', DISPUTED: 'bad', NA: 'mute' }[d.ack_state] || 'info'));
    const admin = isAdmin() && d.state === 'COMPLETED' ? h('div', { class: 'btn-row' },
      d.verify_required && !d.verified_by ? [h('button', { class: 'btn', type: 'button', onClick: () => verifyDialog({ ids: [id], approve: true, onDone: load }) }, icon('checkmark--outline'), 'Verify'), h('button', { class: 'btn', type: 'button', onClick: () => verifyDialog({ ids: [id], approve: false, onDone: load }) }, 'Send back…')] : null,
      d.ack_state === 'PENDING' ? [h('button', { class: 'btn', type: 'button', onClick: () => ownerDialog({ ids: [id], owner: d.owner_name, onDone: load }) }, icon('user'), 'Owner accepts…'), h('button', { class: 'btn', type: 'button', onClick: () => ownerDialog({ ids: [id], dispute: true, onDone: load }) }, 'Owner disputes…')] : null) : null;

    function ownerText() {
      const label = ACK[d.ack_state]?.[0] || d.ack_state;
      if (d.ack_state === 'PENDING') return d.state === 'COMPLETED' ? `Waiting - deemed accepted on ${date(d.ack_due)} if no answer` : 'Starts once the work is signed';
      if (d.ack_state === 'NA') return 'Not applicable';
      return [`${label}${d.ack_by ? ' · ' + d.ack_by : ''}`, h('div', { class: 'faint' }, `${d.ack_at ? when(d.ack_at) : ''}${d.ack_recorded_by && d.ack_state !== 'DEEMED' ? ' · recorded by ' + d.ack_recorded_by : ''}${d.ack_note ? ' · ' + d.ack_note : ''}`)];
    }

    const sign = panel('Sign-off', {}, signoff, admin,
      d.defer_status === 'REQUESTED' ? h('div', { class: 'wo-defer' }, h('strong', null, 'Deferral requested'), h('div', null, `To ${date(d.defer_to)}: ${d.defer_reason}`),
        isAdmin() ? h('div', { class: 'btn-row' }, h('button', { class: 'btn primary', type: 'button', onClick: () => decideDeferral(true) }, 'Approve'), h('button', { class: 'btn', type: 'button', onClick: () => decideDeferral(false) }, 'Decline')) : null) : null,
      isAdmin() && ['OPEN', 'IN_PROGRESS'].includes(d.state) ? h('div', { class: 'btn-row' }, h('button', { class: 'btn danger', type: 'button', onClick: cancelDialog }, 'Cancel work order…')) : null,
      d.state === 'CANCELLED' ? h('p', { class: 'muted' }, `Cancelled: ${d.cancel_reason}`) : null);

    const findings = panel('Findings', { cls: 'span-12', flush: true, hint: d.findings.length ? `${d.findings.filter((f) => f.status === 'OPEN').length} open` : '' }, d.findings.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['Severity', 'Finding', 'Call', 'Status', ''].map((t) => h('th', null, t)))),
      h('tbody', null, d.findings.map((f) => { const [sw, st] = SEVERITY[f.severity];
        return h('tr', null, h('td', null, h('span', { class: 'badge ' + st }, sw)), h('td', null, f.description, f.close_note ? h('div', { class: 'faint' }, `Resolved: ${f.close_note}`) : null),
          h('td', null, f.sr_id ? h('span', { class: 'mono' }, f.sr_id) : f.call_required ? h('span', { class: 'badge warn' }, 'Call needed') : h('span', { class: 'faint' }, '—')),
          h('td', null, f.status === 'OPEN' ? h('span', { class: 'badge warn' }, 'Open') : h('span', { class: 'badge ok' }, 'Closed')),
          h('td', { class: 'right' }, f.status === 'OPEN' && mine() ? h('span', { class: 'btn-row tight' },
            !f.sr_id ? h('button', { class: 'btn', type: 'button', onClick: () => linkCallDialog({ finding: f, onDone: load }) }, icon('link'), 'Link call') : null,
            h('button', { class: 'btn', type: 'button', onClick: () => closeFindingDialog({ finding: f, onDone: load }) }, 'Close'),
            isAdmin() && f.severity !== 'CRITICAL' ? h('button', { class: 'btn ghost', type: 'button', onClick: () => setSeverity(f, f.severity === 'MINOR' ? 'MAJOR' : 'CRITICAL', load).catch((e) => toast(e.message, 'bad')) }, 'Raise severity') : null) : null)); })))) : h('div', { class: 'muted', style: { padding: '16px' } }, 'No findings.'));

    const history = panel('History', { cls: 'span-6', flush: true }, h('ul', { class: 'wo-timeline' }, d.events.map((e) => h('li', null, h('strong', null, EVENTS[e.action] || e.action), e.note ? ` - ${e.note}` : '', h('div', { class: 'faint' }, `${when(e.at)} · ${e.actor}`)))));
    const prev = panel('Earlier PM on this asset', { cls: 'span-6', flush: true }, d.previous.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('tbody', null, d.previous.map((p) => h('tr', null,
      h('td', null, p.quarter_label), h('td', { class: 'mono' }, p.wo_no), h('td', null, p.state === 'CLOSED' || p.state === 'COMPLETED' ? `${date(p.pm_date)} · ${p.done_by ? person(p.done_by) : ''}` : p.state.toLowerCase())))))) : h('div', { class: 'muted', style: { padding: '16px' } }, 'No earlier work orders.'));

    body.replaceChildren(checklist, h('div', { class: 'span-4 stack-v' }, panel('Asset', { flush: true }, facts), sign), findings, history, prev);
  }

  function step(n, title, text, tone) {
    return h('div', { class: 'wo-step ' + tone }, h('span', { class: 'n' }, n), h('div', null, h('div', { class: 'flabel' }, title), h('div', null, text)));
  }

  load();
  return { destroy() { dead = true; } };
}
