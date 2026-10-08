// PM work orders: this quarter's list with the stages a PM goes through, and the batch actions (mark all pass, verify, owner answer).
import { get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../ui/toast.js';
import { date, int, pct, person } from '../core/format.js';
import * as router from '../core/router.js';
import { isAdmin } from '../core/session.js';
import { columnsDialog, loadColumns } from '../ui/columns.js';
import { entity } from '../ui/hovercard.js';
import { openModal } from '../ui/modal.js';
import { errorBlock, kpiStrip, loading, pageHead } from './common.js';
import { frow, ownerDialog, report, stateBadges, today, verifyDialog } from './pm-wo-common.js';

const TABS = [['all', 'All'], ['todo', 'To do'], ['progress', 'In progress'], ['overdue', 'Overdue'], ['verify', 'Awaiting verification'], ['owner', 'Awaiting owner'], ['deferral', 'Deferrals'], ['closed', 'Closed']];
const COUNT_OF = { all: 'total', todo: 'todo', progress: 'progress', overdue: 'overdue', verify: 'verify', owner: 'owner', deferral: 'deferral', closed: 'closed' };
const PAGE = 300;

// Every column the list can show, in the default order. The person's own order/visibility is saved as the "pm_work" view.
const COLS = [
  { key: 'wo_no', label: 'Work order', render: (r) => h('a', { href: `#/pm/wo/${r.wo_id}`, class: 'mono' }, r.wo_no) },
  { key: 'asset', label: 'Asset', render: (r) => [entity('asset', r.asset_key, r.asset_key), r.model ? h('div', { class: 'faint' }, r.model) : null] },
  { key: 'class', label: 'Class', render: (r) => [r.asset_class || '', h('span', { class: 'crit', title: `Criticality ${r.criticality}${r.verify_required ? ' - technical verification required' : ''}` }, r.criticality)] },
  { key: 'engineer', label: 'Engineer', render: (r) => (r.engineer_name ? person(r.engineer_name) : h('span', { class: 'badge bad' }, 'Unassigned')) },
  { key: 'location', label: 'Location', render: (r) => r.location_code || '' },
  { key: 'owner', label: 'Owner', render: (r) => [r.owner_name ? person(r.owner_name) : r.owner_dept || h('span', { class: 'faint' }, '—'), r.owner_kind === 'SECTION' ? h('div', { class: 'faint' }, 'section') : null] },
  { key: 'due', label: 'Due', render: (r) => [date(r.eff_due), r.defer_status === 'APPROVED' ? h('div', { class: 'faint' }, 'deferred') : null] },
  { key: 'stage', label: 'Stage', render: (r) => [stateBadges(r).map(([t, tone]) => h('span', { class: 'badge ' + tone, style: { marginRight: '4px' } }, t)), r.open_findings ? h('span', { class: 'badge bad' }, `${r.open_findings} finding${r.open_findings > 1 ? 's' : ''}`) : null] },
];

export function mountPmWork(root) {
  let dead = false, timer = null, sum = null, rows = [], total = 0, cols = COLS.map((c) => ({ ...c, visible: true }));
  const p = router.current().params;
  const state = { quarter: p.get('quarter') || '', bucket: p.get('bucket') || 'all', q: p.get('q') || '', engineer: p.get('engineer') || '', cls: p.get('cls') || '' };
  const selected = new Set();
  const sub = h('span', null, 'Loading…');
  const tools = h('div', { class: 'btn-row tight' });
  const kpis = h('div');
  const tabs = h('div', { class: 'wo-tabs', role: 'tablist', 'aria-label': 'Stage' });
  const filters = h('div', { class: 'toolbar', role: 'search' });
  const bar = h('div', { class: 'wo-bulk', hidden: true, role: 'region', 'aria-label': 'Selected work orders' });
  const table = h('div', { class: 'tbl-wrap' });
  const more = h('div', { class: 'toolbar', hidden: true });
  root.append(h('div', { class: 'page', 'data-mod': 'pm' }, pageHead('PM work orders', sub, tools), kpis, h('div', { class: 'spacer' }), h('section', { class: 'panel' }, tabs, filters, bar, table, more)));
  table.append(loading());

  const sync = () => router.setParams({ ...state, bucket: state.bucket === 'all' ? '' : state.bucket });
  const args = (extra = {}) => ({ ...(state.quarter ? { quarter: state.quarter } : {}), ...(state.bucket !== 'all' ? { bucket: state.bucket } : {}), ...(state.q ? { q: state.q } : {}), ...(state.engineer ? { engineer: state.engineer } : {}), ...(state.cls ? { cls: state.cls } : {}), ...extra });

  async function loadSummary() {
    sum = await get('/api/pmwo/summary', state.quarter ? { quarter: state.quarter } : {});
    if (dead) return;
    state.quarter = sum.quarter;
    const k = sum.kpi, f = sum.findings;
    sub.textContent = `${sum.quarter} · ${int(k.total)} work orders · due by quarter end${sum.quarter === sum.current ? '' : ' (past quarter)'}`;
    kpis.replaceChildren(kpiStrip([
      { id: 'pct', label: 'Completed', value: pct(k.pct_done), sub: `${int(k.done)} of ${int(k.total)}` },
      { id: 'todo', label: 'To do', value: int(k.todo + k.progress), sub: `${int(k.progress)} in progress` },
      { id: 'over', label: 'Overdue', value: int(k.overdue), tone: k.overdue ? 'bad' : 'ok', sub: 'past due date' },
      { id: 'ver', label: 'Awaiting verification', value: int(k.verify), tone: k.verify ? 'warn' : 'ok', sub: 'second person' },
      { id: 'own', label: 'Awaiting owner', value: int(k.owner), sub: `deemed accepted after ${sum.ack_days} days` },
      { id: 'fin', label: 'Open findings', value: int(f.open), tone: f.critical ? 'bad' : f.open ? 'warn' : 'ok', sub: f.call_needed ? `${int(f.call_needed)} need a call` : 'none need a call', href: '#/pm/findings' },
    ]));
    drawTabs();
  }

  function drawTabs() {
    const k = sum.kpi;
    tabs.replaceChildren(...TABS.map(([id, label]) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(state.bucket === id), class: 'wo-tab', onClick: () => { state.bucket = id; selected.clear(); sync(); drawTabs(); load(); } },
      label, h('span', { class: 'n' }, int(k[COUNT_OF[id]] ?? 0)))));
  }

  function drawFilters() {
    const q = h('input', { type: 'search', class: 'no-upper', placeholder: 'Search work order, asset, engineer, location, owner…', 'aria-label': 'Search', value: state.q, autocomplete: 'off' });
    q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { state.q = q.value.trim(); selected.clear(); sync(); load(); }, 300); });
    const eng = isAdmin() ? h('select', { 'aria-label': 'Engineer', onChange: (e) => { state.engineer = e.target.value; selected.clear(); sync(); load(); } },
      [h('option', { value: '' }, 'Every engineer'), ...sum.by_engineer.map((r) => h('option', { value: r.label, selected: r.label === state.engineer }, r.label === 'UNASSIGNED' ? 'Unassigned' : person(r.label)))]) : null;
    const cls = h('select', { 'aria-label': 'Class', onChange: (e) => { state.cls = e.target.value; selected.clear(); sync(); load(); } },
      [h('option', { value: '' }, 'Every class'), ...sum.by_class.map((r) => h('option', { value: r.label, selected: r.label === state.cls }, r.label))]);
    const qs = sum.quarters.length > 1 ? h('select', { 'aria-label': 'Quarter', onChange: (e) => { state.quarter = e.target.value; selected.clear(); sync(); start(); } },
      sum.quarters.map((l) => h('option', { value: l, selected: l === state.quarter }, l))) : null;
    filters.replaceChildren(...[qs, h('div', { class: 'field' }, q), eng, cls].filter(Boolean));
  }

  function drawTools() {
    tools.replaceChildren(...[
      isAdmin() ? h('button', { class: 'btn', type: 'button', onClick: generate }, icon('add'), sum.generated ? 'Create missing work orders' : 'Create this quarter\'s work orders') : null,
      h('button', { class: 'btn', type: 'button', onClick: () => columnsDialog({ name: 'pm_work', columns: cols, onApply: (next) => { cols = next || COLS.map((c) => ({ ...c, visible: true })); drawTable(); } }) }, icon('settings'), 'Columns'),
      h('a', { class: 'btn', href: '#/pm/findings' }, icon('warning'), 'Findings'), h('a', { class: 'btn', href: '#/pm/checklists' }, icon('document'), 'Checklists'), h('a', { class: 'btn', href: '#/pm' }, icon('arrow--left', 'sm'), 'Dashboard')].filter(Boolean));
  }

  async function generate() {
    try { const r = await send('/api/pmwo/generate', {}); toast(r.created ? `${int(r.created)} work orders created${r.legacy ? ` (${int(r.legacy)} already done this quarter)` : ''}` : 'Nothing to create - every asset already has its work order'); start(); } catch (e) { toast(e.message, 'bad'); }
  }

  async function load(offset = 0) {
    if (!offset) { table.replaceChildren(loading()); more.hidden = true; }
    try {
      const d = await get('/api/pmwo/list', args({ limit: PAGE, offset }));
      if (dead) return;
      rows = offset ? rows.concat(d.rows) : d.rows;
      total = d.total;
      drawTable();
    } catch (e) { if (!dead) table.replaceChildren(errorBlock(e, () => load())); }
  }

  function drawBar() {
    const n = selected.size;
    bar.hidden = !n;
    if (!n) return;
    const ids = [...selected];
    const b = state.bucket;
    const acts = [];
    if (['all', 'todo', 'progress', 'overdue'].includes(b)) acts.push(h('button', { class: 'btn primary', type: 'button', onClick: () => batchDialog(ids) }, icon('checkmark'), 'Mark all lines Pass…'));
    if (isAdmin() && ['all', 'verify'].includes(b)) acts.push(h('button', { class: 'btn', type: 'button', onClick: () => verifyDialog({ ids, approve: true, onDone: start }) }, icon('checkmark--outline'), 'Verify'), h('button', { class: 'btn', type: 'button', onClick: () => verifyDialog({ ids, approve: false, onDone: start }) }, 'Send back…'));
    if (isAdmin() && ['all', 'owner'].includes(b)) acts.push(h('button', { class: 'btn', type: 'button', onClick: () => ownerDialog({ ids, owner: ids.length === 1 ? rows.find((r) => r.wo_id === ids[0])?.owner_name : '', onDone: start }) }, icon('user'), 'Owner accepts…'), h('button', { class: 'btn', type: 'button', onClick: () => ownerDialog({ ids, dispute: true, onDone: start }) }, 'Owner disputes…'));
    bar.replaceChildren(h('strong', null, `${n} selected`), ...acts, h('button', { class: 'btn ghost', type: 'button', onClick: () => { selected.clear(); drawTable(); } }, 'Clear'));
  }

  function batchDialog(ids) {
    const d = h('input', { id: 'bt-d', type: 'date', value: today(), max: today() });
    const m = h('input', { id: 'bt-m', type: 'number', min: '0', max: '1440', placeholder: 'Minutes per asset (optional)' });
    openModal({
      title: `Mark every line Pass for ${ids.length} work order${ids.length > 1 ? 's' : ''}`,
      lead: 'Use this only for assets you have checked. Lines you already answered (including any Fail) are kept. Work orders that need a reading, such as a server temperature, are skipped. The batch is recorded as such in each work order\'s history.',
      body: h('div', null, frow('bt-d', 'PM done on', d), frow('bt-m', 'Time', m)),
      actions: [{ label: 'Cancel' }, { label: 'Complete and sign', primary: true, icon: 'checkmark', onClick: async () => { const r = await send('/api/pmwo/batch', { ids, pm_date: d.value, minutes: m.value }); report(r, 'completed'); selected.clear(); start(); } }],
    });
  }

  const shown = () => cols.filter((c) => c.visible);

  function drawTable() {
    drawBar();
    if (!rows.length) { table.replaceChildren(h('div', { class: 'muted', style: { padding: '16px' } }, sum.kpi.total ? 'Nothing matches.' : (isAdmin() ? 'No work orders yet for this quarter. Use "Create this quarter\'s work orders".' : 'No work orders have been created for this quarter yet.'))); more.hidden = true; return; }
    const all = h('input', { type: 'checkbox', 'aria-label': 'Select all shown', checked: rows.length && rows.every((r) => selected.has(r.wo_id)) });
    all.addEventListener('change', () => { rows.forEach((r) => (all.checked ? selected.add(r.wo_id) : selected.delete(r.wo_id))); drawTable(); });
    table.replaceChildren(h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, h('th', { style: { width: '32px' } }, all), ...shown().map((c) => h('th', null, c.label)))),
      h('tbody', null, rows.map((r) => {
        const cb = h('input', { type: 'checkbox', 'aria-label': `Select ${r.wo_no}`, checked: selected.has(r.wo_id) });
        cb.addEventListener('change', () => { if (cb.checked) selected.add(r.wo_id); else selected.delete(r.wo_id); drawBar(); });
        return h('tr', null, h('td', null, cb), ...shown().map((c) => h('td', null, c.render(r))));
      }))));
    more.hidden = rows.length >= total;
    more.replaceChildren(h('span', { class: 'muted' }, `Showing ${int(rows.length)} of ${int(total)}`), h('button', { class: 'btn', type: 'button', onClick: () => load(rows.length) }, 'Show more'));
  }

  async function start() {
    try { cols = await loadColumns('pm_work', COLS); await loadSummary(); if (dead) return; drawTools(); drawFilters(); await load(); } catch (e) { if (!dead) table.replaceChildren(errorBlock(e, start)); }
  }
  start();
  return { onLive: () => { loadSummary().catch(() => {}); }, destroy() { dead = true; clearTimeout(timer); } };
}
