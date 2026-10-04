// Past quarters: every quarter that was frozen when it closed, with the asset-by-asset picture, who did the PM, and a download.
import { get } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { date, int, pct, person } from '../core/format.js';
import * as router from '../core/router.js';
import { entity } from '../ui/hovercard.js';
import { shareMenu } from '../ui/share.js';
import { barRows, errorBlock, kpiStrip, loading, pageHead, panel } from './common.js';
import { when } from './audit.js';

const WORDS = { PENDING: ['Scheduled', 'mute'], DONE: ['Completed', 'ok'], DONE_OUTSIDE_QUARTER: ['Completed late', 'warn'] };

export function mountPmHistory(root) {
  let dead = false, quarters = [], timer = null;
  const params = router.current().params;
  const state = { quarter: params.get('quarter') || '', as_of: params.get('as_of') || '', status: params.get('status') || '', q: params.get('q') || '' };
  const sub = h('span', null, 'Quarters that have been closed, exactly as they stood when they were frozen.');
  const list = h('div', { class: 'grid' });
  const filters = h('div', { class: 'toolbar', role: 'search' });
  const detail = h('div', { class: 'grid' });
  const tools = [h('a', { class: 'btn', href: '#/pm' }, icon('arrow--left', 'sm'), 'Dashboard')];
  root.append(h('div', { class: 'page', 'data-mod': 'pm' }, pageHead('Past quarters', sub, tools), list, h('div', { class: 'spacer' }), filters, detail));
  list.append(loading());

  const sync = () => router.setParams(state);

  async function loadList() {
    try {
      const d = await get('/api/pm/history');
      if (dead) return;
      quarters = d.quarters;
      if (!quarters.length) { list.replaceChildren(h('div', { class: 'span-12 muted' }, 'No quarter has been closed yet. A snapshot is frozen when a quarter is closed (Cycles and snapshots).')); filters.hidden = true; return; }
      if (!quarters.some((x) => x.label === state.quarter)) { state.quarter = quarters[0].label; state.as_of = ''; }
      drawList();
      drawFilters();
      await loadDetail();
    } catch (e) { if (!dead) list.replaceChildren(h('div', { class: 'span-12' }, errorBlock(e, loadList))); }
  }

  function drawList() {
    const pick = (label) => { state.quarter = label; state.as_of = ''; state.status = ''; state.q = ''; sync(); drawList(); drawFilters(); loadDetail(); };
    list.replaceChildren(panel('Closed quarters', { cls: 'span-12', flush: true, hint: 'select a quarter to open it' }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['Quarter', 'Period', 'Closed', 'In scope', 'Completed', 'Completed late', 'Scheduled (not done)', 'Done %', ''].map((t, i) => h('th', { class: i > 2 && i < 8 ? 'right' : '' }, t)))),
      h('tbody', null, quarters.map((r) => {
        const on = r.label === state.quarter;
        return h('tr', { 'aria-selected': String(on), class: on ? 'sel' : '' },
          h('td', null, h('button', { class: 'link-btn', type: 'button', 'aria-current': on ? 'true' : null, onClick: () => pick(r.label) }, r.label)),
          h('td', null, r.start ? `${date(r.start)} → ${date(r.end)}` : '—'),
          h('td', null, r.closed_at ? [when(r.closed_at), r.closed_by ? ` · ${r.closed_by}` : ''] : r.status === 'OPEN' ? h('span', { class: 'badge info' }, 'Open') : h('span', { class: 'faint' }, 'snapshots only')),
          h('td', { class: 'right' }, int(r.scope)), h('td', { class: 'right' }, int(r.done)), h('td', { class: 'right' }, int(r.stale)), h('td', { class: 'right' }, int(r.pending)), h('td', { class: 'right' }, pct(r.pct_done)),
          h('td', { class: 'right' }, on ? h('span', { class: 'badge info' }, 'Showing') : null));
      }))))));
  }

  function drawFilters(snaps = []) {
    const status = h('select', { 'aria-label': 'PM status', onChange: (e) => { state.status = e.target.value; sync(); loadDetail(); } },
      [['', 'Every status'], ['DONE', 'Completed'], ['DONE_OUTSIDE_QUARTER', 'Completed late'], ['PENDING', 'Scheduled (not done)']].map(([v, t]) => h('option', { value: v, selected: v === state.status }, t)));
    const q = h('input', { type: 'search', class: 'no-upper', placeholder: 'Search asset, engineer, location, class, signer…', 'aria-label': 'Search this quarter', value: state.q, autocomplete: 'off' });
    q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { state.q = q.value.trim(); sync(); loadDetail(); }, 300); });
    const asOf = h('select', { 'aria-label': 'Snapshot date', onChange: (e) => { state.as_of = e.target.value; sync(); loadDetail(); } },
      snaps.map((d, i) => h('option', { value: String(d).slice(0, 10), selected: String(d).slice(0, 10) === state.as_of || (!state.as_of && i === 0) }, `${date(d)}${i === 0 ? ' (latest)' : ''}`)));
    filters.replaceChildren(h('span', { class: 'muted' }, state.quarter), h('label', { class: 'flabel' }, 'Snapshot ', asOf), status, q);
  }

  const kv = (label, value, tone) => ({ id: label, label, value, tone });

  async function loadDetail() {
    detail.replaceChildren(loading());
    try {
      const d = await get('/api/pm/history/detail', { quarter: state.quarter, ...(state.as_of ? { as_of: state.as_of } : {}), ...(state.status ? { status: state.status } : {}), ...(state.q ? { q: state.q } : {}) });
      if (dead || d.label !== state.quarter) return;
      if (filters.querySelector('select[aria-label="Snapshot date"]')?.options.length !== d.snapshots.length) drawFilters(d.snapshots);
      sub.textContent = `${d.label} · snapshot of ${date(d.as_of)} · ${int(d.kpi.scope)} assets were in scope`;
      const k = d.kpi;
      const body = (ra) => h('div', { class: 'tbl-wrap' }, ra);
      const head = (cols) => h('thead', null, h('tr', null, cols.map((t, i) => h('th', { class: i ? 'right' : '' }, t))));
      const brk = (rows, link) => body(h('table', { class: 'tbl' }, head(['', 'In scope', 'Completed', 'Late', 'Scheduled']),
        h('tbody', null, rows.map((r) => h('tr', null, h('td', null, link ? link(r.label) : r.label), h('td', { class: 'right' }, int(r.scope)), h('td', { class: 'right' }, int(r.done)), h('td', { class: 'right' }, int(r.stale)), h('td', { class: 'right' }, int(r.pending)))))));
      const a = d.assets;
      const note = a.total > a.rows.length ? `Showing the first ${int(a.rows.length)} of ${int(a.total)}. Narrow it with the filters, or download the whole list.` : `${int(a.total)} assets`;
      const dl = shareMenu({ formats: ['xlsx', 'csv', 'pdf'], endpoint: '/api/pm/history/export', mail: false, body: () => ({ quarter: d.label, as_of: d.as_of, status: state.status, q: state.q }) });
      detail.replaceChildren(
        h('div', { class: 'span-12' }, kpiStrip([
          kv('Assets in scope', int(k.scope)), kv('PM completed', int(k.done), k.done ? 'ok' : null), kv('Completed late', int(k.stale), k.stale ? 'warn' : 'ok'),
          kv('Scheduled, not done', int(k.pending), k.pending ? 'bad' : 'ok'), kv('Done overall', pct(k.pct_done)), kv('Scheduled, no engineer', int(k.unassigned), k.unassigned ? 'bad' : 'ok'),
        ])),
        panel('By engineer', { cls: 'span-8', flush: true }, brk(d.by_engineer, (l) => (l === 'UNASSIGNED' ? h('span', { class: 'badge bad' }, 'Unassigned') : person(l)))),
        panel('Scheduled, not done - by location', { cls: 'span-4' }, d.by_location.some((r) => r.pending) ? barRows(d.by_location.filter((r) => r.pending).slice(0, 12).map((r) => ({ label: r.label, n: r.pending, color: 'var(--chart-8)' }))) : h('div', { class: 'muted' }, 'Nothing was left undone.')),
        panel('By asset class', { cls: 'span-6', flush: true }, brk(d.by_class)),
        panel('PM recorded in the portal this quarter', { cls: 'span-6', flush: true, hint: `${int(d.recorded.length)} entries` }, d.recorded.length ? body(h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Asset', 'Date', 'Done by', 'Signed by', 'Recorded by'].map((t) => h('th', null, t)))),
          h('tbody', null, d.recorded.map((r) => h('tr', null, h('td', null, entity('asset', r.asset_key, r.asset_key)), h('td', null, date(r.pm_date)), h('td', null, person(r.done_by || '')), h('td', null, person(r.signed_by || '')), h('td', null, person(r.recorded_by || '')))))))
          : h('div', { class: 'muted', style: { padding: '16px' } }, 'Nothing was recorded through the portal in this quarter (PM dates may have come from an import).')),
        panel(`Assets in ${d.label}`, { cls: 'span-12', flush: true, hint: note }, h('div', { class: 'panel-tools', style: { padding: '8px 12px' } }, dl), a.rows.length ? body(h('table', { class: 'tbl' },
          h('thead', null, h('tr', null, ['Asset', 'Class', 'Engineer', 'Location', 'PM status', 'PM date', 'Done by', 'Signed by'].map((t) => h('th', null, t)))),
          h('tbody', null, a.rows.map((r) => { const [w, tone] = WORDS[r.pm_status] || [r.pm_status, 'mute'];
            return h('tr', null, h('td', null, entity('asset', r.asset_key, r.asset_key)), h('td', null, r.asset_class || ''), h('td', null, r.engineer_name ? person(r.engineer_name) : h('span', { class: 'faint' }, 'Unassigned')), h('td', null, r.location_code || ''),
              h('td', null, h('span', { class: 'badge ' + tone }, w)), h('td', null, r.pm_date ? date(r.pm_date) : h('span', { class: 'faint' }, '—')), h('td', null, person(r.pm_done_by || '')), h('td', null, person(r.pm_signed_by || ''))); })))) : h('div', { class: 'muted', style: { padding: '16px' } }, 'No asset matches these filters.')),
      );
    } catch (e) { if (!dead) detail.replaceChildren(h('div', { class: 'span-12' }, errorBlock(e, loadDetail))); }
  }

  loadList();
  return { destroy() { dead = true; clearTimeout(timer); } };
}
