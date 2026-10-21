// AMC removal requests: every request with its assets, who asked and approved, and the credit that stands; reinstate or cancel from here; Excel report.
// Administrators and Team Leader/SI. See portal/app/amcremoval.py.
import { downloadGet, get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { date, int } from '../core/format.js';
import { toast } from '../ui/toast.js';
import { errorBlock, kpiStrip, pageHead, panel } from './common.js';
import { amcReinstateDialog } from './amc-removal.js';
import { user as sessionUser } from '../core/session.js';
import { statusChip } from './amc-list.js';

const inr = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const STATES = [['', 'All requests'], ['REQUESTED', 'Waiting for a decision'], ['PENDING', 'Approved - waiting for its date'], ['APPLIED', 'Removed'], ['REVERSED', 'Reinstated'], ['REJECTED', 'Turned down'], ['CANCELLED', 'Withdrawn']];

export function mountAmcRequests(root) {
  let dead = false, status = '', quarter = '', meta = null;
  const head = h('div'), table = h('div');
  const sel = h('select', { 'aria-label': 'Show requests' }, STATES.map(([v, t]) => h('option', { value: v }, t)));
  sel.addEventListener('change', () => { status = sel.value; load(); });
  const exportBtn = h('button', { class: 'btn', type: 'button', title: 'An Excel workbook of every removal line: the paper behind a credit note', onClick: async () => {
    try { await downloadGet('/api/amc-removal/export', quarter ? { quarter } : {}, 'AMC_removals.xlsx'); toast('Downloaded'); } catch (e) { toast(e.message, 'bad'); }
  } }, icon('download'), 'Excel report');
  root.append(h('div', { class: 'page' }, pageHead('AMC removal requests', 'Every request, what it removed, who asked and approved, and the credit due from the vendor.', [sel, exportBtn]), head, table));

  async function load() {
    try {
      const [s, r, m] = await Promise.all([get('/api/amc-removal/summary'), get('/api/amc-removal/requests', status ? { status } : {}), meta ? Promise.resolve(meta) : get('/api/amc-removal/meta')]);
      meta = m;
      if (!dead) draw(s, r.rows);
    } catch (e) { if (!dead) table.replaceChildren(errorBlock(e, load)); }
  }

  function draw(s, rows) {
    const cur = s.quarters[0];
    head.replaceChildren(kpiStrip([
      { id: 'wait', label: 'Waiting for a decision', value: int(s.waiting), tone: s.waiting ? 'warn' : 'ok', sub: 'engineer requests' },
      { id: 'pend', label: 'Approved, waiting for date', value: int(s.pending), sub: 'applied automatically' },
      { id: 'rem', label: cur ? `Removed · ${cur.quarter_label}` : 'Removed', value: int(cur ? cur.assets : 0), sub: cur && cur.parts ? `+ ${int(cur.parts)} parts` : 'assets' },
      { id: 'cr', label: cur ? `Credit due · ${cur.quarter_label}` : 'Credit due', value: inr(cur ? cur.credit : 0), sub: 'after any reinstatement' },
    ]), panel('By quarter', { hint: 'click a quarter to limit the Excel report to it' }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['Quarter', 'Assets removed', 'Parts', 'Reinstated', 'Credit that stands'].map((t) => h('th', null, t)))),
      h('tbody', null, s.quarters.length ? s.quarters.map((q) => h('tr', { style: { cursor: 'pointer' }, class: quarter === q.quarter_label ? 'sel' : null, onClick: () => { quarter = quarter === q.quarter_label ? '' : q.quarter_label; draw(s, rows); } },
        h('td', null, q.quarter_label), h('td', { class: 'num' }, int(q.assets)), h('td', { class: 'num' }, int(q.parts)), h('td', { class: 'num' }, int(q.reinstated)), h('td', { class: 'num' }, inr(q.credit)))) : [h('tr', null, h('td', { colspan: '5', class: 'muted' }, 'No assets have been removed yet.'))])))));
    exportBtn.title = quarter ? `Excel report for ${quarter}` : 'An Excel workbook of every removal line: the paper behind a credit note';
    table.replaceChildren(...(rows.length ? rows.map(card) : [h('p', { class: 'muted' }, 'No requests.')]));
  }

  function card(r) {
    const own = r.lines.filter((x) => !x.via_parent);
    const parts = r.lines.filter((x) => x.via_parent);
    const back = own.filter((x) => !x.reinstated_at && !x.skipped_reason && r.status === 'APPLIED');
    // Which assets of this request go back is the person's choice: a request can hold many assets and only some may be needed again.
    // With several to choose from nothing is ticked at first, so a stray click never reinstates the lot. Parts always return with their asset.
    const chosen = new Set(back.length === 1 ? [back[0].asset_key] : []);
    const boxes = new Map();
    const reinstateBtn = h('button', { class: 'btn', type: 'button', onClick: () => amcReinstateDialog({ keys: [...chosen], onDone: load }) });
    const paint = () => {
      reinstateBtn.replaceChildren(icon('renew'), chosen.size ? `Reinstate ${chosen.size === 1 ? [...chosen][0] : chosen.size + ' selected assets'}` : 'Reinstate selected');
      reinstateBtn.disabled = !chosen.size;
      boxes.forEach((cb, k) => { cb.checked = chosen.has(k); });
      if (all) { all.checked = chosen.size === back.length; all.indeterminate = chosen.size > 0 && chosen.size < back.length; }
    };
    const all = back.length > 1 ? h('input', { type: 'checkbox', 'aria-label': 'Select every asset that can be reinstated' }) : null;
    all?.addEventListener('change', () => { back.forEach((x) => (all.checked ? chosen.add(x.asset_key) : chosen.delete(x.asset_key))); paint(); });
    const pick = (x) => {
      if (!back.includes(x)) return h('td', null);
      const cb = h('input', { type: 'checkbox', 'aria-label': `Select ${x.asset_key} to reinstate` });
      cb.addEventListener('change', () => { if (cb.checked) chosen.add(x.asset_key); else chosen.delete(x.asset_key); paint(); });
      boxes.set(x.asset_key, cb);
      return h('td', null, cb);
    };
    const tools = h('div', { class: 'btn-row' },
      back.length ? reinstateBtn : null,
      ['PENDING', 'REQUESTED'].includes(r.status) ? h('button', { class: 'btn', type: 'button', onClick: async () => { try { await send('/api/amc-removal/cancel', { removal_no: r.removal_no }); toast('Request cancelled'); load(); } catch (e) { toast(e.message, 'bad'); } } }, icon('close'), 'Cancel request') : null);
    const decide = r.status === 'REQUESTED' ? decision(r) : null;
    const rows = r.lines.map((x) => h('tr', null, ...(back.length ? [pick(x)] : []), h('td', { class: 'mono' }, x.asset_key, x.via_parent ? h('span', { class: 'muted small' }, ` (comes back with ${x.via_parent})`) : null),
      h('td', null, x.via_parent ? '' : x.reason.replace(/_/g, ' ').toLowerCase()), h('td', { class: 'num' }, x.reinstated_at ? int(x.net_credit_days) : x.credit_days == null ? '—' : int(x.credit_days)),
      h('td', { class: 'num' }, inr(x.reinstated_at ? x.net_credit_amount : x.credit_amount)), h('td', null, x.skipped_reason ? `skipped: ${x.skipped_reason}` : x.reinstated_at ? `reinstated ${date(x.reinstated_at)}` : '')));
    const el = h('details', { class: 'panel', style: { padding: '8px 12px', marginBottom: '8px' }, open: r.status === 'REQUESTED' },
      h('summary', null, h('strong', { class: 'mono' }, r.removal_no), ' ', statusChip(r.status), ` · ${int(own.length)} asset(s)${parts.length ? ` + ${parts.length} parts` : ''} · effective ${date(r.effective_date)} · credit ${inr(r.credit_total)}`),
      h('p', { class: 'muted small' }, `Requested by ${r.requested_by}${r.approved_by ? ` · verbal approval by ${r.approved_by} on ${date(r.approved_on)}` : ''}${r.decided_by && r.decided_by !== r.requested_by ? ` · decided by ${r.decided_by}` : ''}${r.note ? ` · "${r.note}"` : ''}${r.decision_note ? ` · turned down: ${r.decision_note}` : ''}`),
      h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ...(back.length ? [h('th', null, all)] : []), ...['Asset (CI)', 'Reason', 'Days', 'Credit', 'Note'].map((t) => h('th', null, t)))), h('tbody', null, rows))),
      back.length > 1 ? h('p', { class: 'muted small' }, 'Tick the assets to put back into the AMC. The others stay removed.') : null, decide, tools);
    paint();
    return el;
  }

  /** An engineer's request waits for a decision: approve it (verbal approver, date, override note) or turn it down with a reason the engineer reads. */
  function decision(r) {
    const me = sessionUser()?.username;
    const who = h('select', { 'aria-label': 'Verbal approval given by' }, h('option', { value: '' }, 'Choose who approved'), meta.approvers.map((a) => h('option', { value: a.username }, `${a.display_name} (${a.username})`)));
    if (me && meta.approvers.some((a) => a.username === me)) who.value = me;
    const when = h('input', { type: 'date', min: meta.quarter.start, max: meta.quarter.end, value: r.effective_date < meta.quarter.start ? meta.quarter.start : r.effective_date, 'aria-label': 'Effective date' });
    const ovr = h('input', { type: 'text', maxlength: '300', placeholder: 'Override note (only if it still has open calls)', 'aria-label': 'Override note' });
    const why = h('input', { type: 'text', maxlength: '300', placeholder: 'Reason for turning it down (the engineer reads this)', 'aria-label': 'Reason for turning it down' });
    const run = async (fn, msg) => { try { await fn(); toast(msg); load(); } catch (e) { toast(e.message, 'bad'); } };
    return h('div', { class: 'rec-note warn', style: { display: 'block', marginBottom: '8px' } },
      h('div', { class: 'btn-row' }, who, when, ovr, h('button', { class: 'btn primary', type: 'button', onClick: () => run(() => send('/api/amc-removal/approve', { removal_no: r.removal_no, approved_by: who.value, effective_date: when.value, override_note: ovr.value }), `${r.removal_no} approved`) }, icon('checkmark'), 'Approve')),
      h('div', { class: 'btn-row', style: { marginTop: '8px' } }, why, h('button', { class: 'btn', type: 'button', onClick: () => run(() => send('/api/amc-removal/reject', { removal_no: r.removal_no, reason: why.value }), `${r.removal_no} turned down`) }, icon('close'), 'Turn down')));
  }

  load();
  return { destroy() { dead = true; } };
}
