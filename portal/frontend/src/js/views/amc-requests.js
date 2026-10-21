// AMC removal requests: every request with its assets, who asked and approved, and the credit that stands; reinstate or cancel from here; Excel report.
// Administrators and Team Leader/SI. See portal/app/amcremoval.py.
import { downloadGet, get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { date, int } from '../core/format.js';
import { toast } from '../ui/toast.js';
import { errorBlock, kpiStrip, pageHead, panel } from './common.js';
import { amcReinstateDialog } from './amc-removal.js';
import { statusChip } from './amc-list.js';

const inr = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const STATES = [['', 'All requests'], ['REQUESTED', 'Waiting for a decision'], ['PENDING', 'Approved - waiting for its date'], ['APPLIED', 'Removed'], ['REVERSED', 'Reinstated'], ['REJECTED', 'Turned down'], ['CANCELLED', 'Withdrawn']];

export function mountAmcRequests(root) {
  let dead = false, status = '', quarter = '';
  const head = h('div'), table = h('div');
  const sel = h('select', { 'aria-label': 'Show requests' }, STATES.map(([v, t]) => h('option', { value: v }, t)));
  sel.addEventListener('change', () => { status = sel.value; load(); });
  const exportBtn = h('button', { class: 'btn', type: 'button', title: 'An Excel workbook of every removal line: the paper behind a credit note', onClick: async () => {
    try { await downloadGet('/api/amc-removal/export', quarter ? { quarter } : {}, 'AMC_removals.xlsx'); toast('Downloaded'); } catch (e) { toast(e.message, 'bad'); }
  } }, icon('download'), 'Excel report');
  root.append(h('div', { class: 'page' }, pageHead('AMC removal requests', 'Every request, what it removed, who asked and approved, and the credit due from the vendor.', [sel, exportBtn]), head, table));

  async function load() {
    try {
      const [s, r] = await Promise.all([get('/api/amc-removal/summary'), get('/api/amc-removal/requests', status ? { status } : {})]);
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
    const tools = h('div', { class: 'btn-row' },
      back.length ? h('button', { class: 'btn', type: 'button', onClick: () => amcReinstateDialog({ keys: back.map((x) => x.asset_key), onDone: load }) }, icon('renew'), `Reinstate ${back.length === 1 ? back[0].asset_key : back.length + ' assets'}`) : null,
      ['PENDING', 'REQUESTED'].includes(r.status) ? h('button', { class: 'btn', type: 'button', onClick: async () => { try { await send('/api/amc-removal/cancel', { removal_no: r.removal_no }); toast('Request cancelled'); load(); } catch (e) { toast(e.message, 'bad'); } } }, icon('close'), 'Cancel request') : null);
    return h('details', { class: 'panel', style: { padding: '8px 12px', marginBottom: '8px' } },
      h('summary', null, h('strong', { class: 'mono' }, r.removal_no), ' ', statusChip(r.status), ` · ${int(own.length)} asset(s)${parts.length ? ` + ${parts.length} parts` : ''} · effective ${date(r.effective_date)} · credit ${inr(r.credit_total)}`),
      h('p', { class: 'muted small' }, `Requested by ${r.requested_by}${r.approved_by ? ` · verbal approval by ${r.approved_by} on ${date(r.approved_on)}` : ''}${r.decided_by && r.decided_by !== r.requested_by ? ` · decided by ${r.decided_by}` : ''}${r.note ? ` · "${r.note}"` : ''}${r.decision_note ? ` · turned down: ${r.decision_note}` : ''}`),
      h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Asset (CI)', 'Reason', 'Days', 'Credit', 'Note'].map((t) => h('th', null, t)))),
        h('tbody', null, r.lines.map((x) => h('tr', null, h('td', { class: 'mono' }, x.asset_key, x.via_parent ? h('span', { class: 'muted small' }, ` (part of ${x.via_parent})`) : null), h('td', null, x.via_parent ? '' : x.reason.replace(/_/g, ' ').toLowerCase()),
          h('td', { class: 'num' }, x.reinstated_at ? int(x.net_credit_days) : x.credit_days == null ? '—' : int(x.credit_days)), h('td', { class: 'num' }, inr(x.reinstated_at ? x.net_credit_amount : x.credit_amount)),
          h('td', null, x.skipped_reason ? `skipped: ${x.skipped_reason}` : x.reinstated_at ? `reinstated ${date(x.reinstated_at)}` : '')))))), tools);
  }

  load();
  return { destroy() { dead = true; } };
}
