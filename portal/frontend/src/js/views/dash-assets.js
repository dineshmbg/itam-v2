import { get } from '../core/api.js';
import { h, icon, clear } from '../core/dom.js';
import { date, int, pct, humanize, clock } from '../core/format.js';
import { badge, labelOf } from '../ui/badge.js';
import * as charts from '../ui/charts.js';
import { shareMenu } from '../ui/share.js';
import { entity } from '../ui/hovercard.js';
import { person } from '../core/format.js';
import { barRows, canvas, errorBlock, kpiStrip, kpiUpdate, legend, pageHead, panel, regHref, loading, NOISE } from './common.js';

const STATUS_COLOR = { IN_USE: 2, IN_STORE: 0, SURPLUS: 6, NOT_IN_USE: 4, NOT_ON_NETWORK: 1, STANDBY: 5, REMOVED_FROM_AMC: 7, TRANSFERRED: 3 };
const COVER_COLOR = { ACTIVE: 2, EXPIRING_90D: 1, EXPIRED: 7, REMOVED: 6, UNKNOWN: 4 };


export function mountAssetsDash(root) {
  let strip, ch = {}, dead = false, data, extra;
  const sub = h('span', null, '');
  const head = pageHead('Assets', sub, shareMenu({ pack: 'assets' }));
  const body = h('div', { class: 'grid' });
  root.append(h('div', { class: 'page', 'data-mod': 'assets' }, head, h('div', { id: 'kpis' }), body));
  body.append(loading());

  const kpis = (d) => {
    const k = d.kpi;
    return [
      { id: 'assets', label: 'Assets', value: int(k.assets), sub: `+ ${int(k.components)} components`, ico: 'devices', href: regHref('assets') },
      { id: 'inuse', label: 'Deployed', value: int(k.in_use), sub: `${pct(100 * k.in_use / k.assets)} of assets`, tone: 'ok', href: regHref('assets', { 'f.asset_status': 'IN_USE' }) },
      { id: 'exp', label: 'Cover expired', value: int(k.cover_expired), sub: 'AMC / warranty ended', tone: k.cover_expired ? 'bad' : 'ok', href: regHref('assets', { 'f.cover_status': 'EXPIRED' }) },
      { id: 'soon', label: 'Cover renewal due (≤ 90 days)', value: int(k.cover_expiring), sub: 'renewal planning', tone: k.cover_expiring ? 'warn' : 'ok', href: regHref('assets', { 'f.cover_status': 'EXPIRING_90D' }) },
      { id: 'pm', label: 'PM scheduled', value: int(k.pm_pending), sub: k.pm_quarter ? humanize(k.pm_quarter) : '', tone: k.pm_pending ? 'warn' : 'ok', href: regHref('assets', { 'f.pm_status': 'PENDING' }) },
      { id: 'users', label: 'Users not in HR', value: int(k.users_inactive), sub: 'retired / wrong CPF', tone: k.users_inactive ? 'warn' : 'ok', href: regHref('assets', { 'f.user_hr_status': 'NOT_IN_HR_MASTER' }) },
    ];
  };

  async function build(d) {
    const k = d.kpi;
    strip = kpiStrip(kpis(d));
    root.querySelector('#kpis').replaceWith(strip);
    strip.id = 'kpis';
    sub.textContent = `Register snapshot ${date(k.as_of)} · ${int(k.assets)} assets and ${int(k.components)} components`;
    const cls = d.by_class;
    const stCols = d.status.map((s) => STATUS_COLOR[s.label] ?? 6);
    const cvCols = d.cover.map((s) => COVER_COLOR[s.label] ?? 6);

    const cClass = canvas('Assets by class, stacked by in use, idle and other', 'tall');
    const cStatus = canvas('Assets by status');
    const cPm = canvas('Preventive maintenance by asset class', 'tall');
    const cLoc = canvas('Top locations by asset count');
    body.append(
      panel('Assets by class', { cls: 'span-7', hint: 'select a bar to open the register' }, cClass, legend([{ label: 'In use', n: cls.reduce((a, r) => a + r.in_use, 0), color: 'var(--chart-3)' }, { label: 'Idle (store / surplus / not used)', n: cls.reduce((a, r) => a + r.idle, 0), color: 'var(--chart-1)' }, { label: 'Other', n: cls.reduce((a, r) => a + r.other, 0), color: 'var(--chart-2)' }])),
      panel('Status', { cls: 'span-5' }, cStatus, legend(d.status.map((s, i) => ({ label: labelOf(s.label), n: s.n, color: `var(--chart-${(stCols[i] % 8) + 1})` })), (it, i) => regHref('assets', { 'f.asset_status': d.status[i].label }))),
      panel('AMC / warranty cover', { cls: 'span-4', hint: 'all records' }, barRows(d.cover.map((c, i) => ({ label: labelOf(c.label), n: c.n, color: `var(--chart-${(cvCols[i] % 8) + 1})`, href: regHref('assets', { 'f.cover_status': c.label, 'f.record_level': '' }) })))),
      panel('Preventive maintenance this quarter', { cls: 'span-8', hint: k.pm_quarter ? humanize(k.pm_quarter) : '' }, cPm, legend([{ label: 'Completed in quarter', n: d.pm.reduce((a, r) => a + r.done, 0), color: 'var(--chart-3)' }, { label: 'Completed late (outside quarter)', n: d.pm.reduce((a, r) => a + r.stale, 0), color: 'var(--chart-5)' }, { label: 'Scheduled', n: d.pm.reduce((a, r) => a + r.pending, 0), color: 'var(--chart-8)' }])),
      panel('Top locations', { cls: 'span-4' }, cLoc),
      panel('Operating systems', { cls: 'span-4' }, barRows(d.os.map((o) => ({ label: humanize(o.label), n: o.n, href: regHref('assets', { 'f.os_family': o.label }) })))),
      panel('Most common data-quality flags', { cls: 'span-4', hint: 'open the register with the flag applied' }, barRows(d.flags.map((f) => ({ label: humanize(f.label), n: f.n, color: 'var(--chart-2)', href: regHref('assets', { 'f.dq_flags': f.label, 'f.record_level': '' }) })))),
      panel('Cover needing attention', { cls: 'span-4', hint: 'oldest expiry first', flush: true }, attention(d)),
    );
    extra = h('div', { class: 'contents' });
    body.append(extra);
    drawExtra(d);

    ch.class = await charts.barH(cClass.querySelector('canvas'), cls.map((r) => humanize(r.label)), [{ label: 'Deployed', data: cls.map((r) => r.in_use), color: 2 }, { label: 'Idle', data: cls.map((r) => r.idle), color: 0 }, { label: 'Other', data: cls.map((r) => r.other), color: 1 }],
      { stacked: true, onPick: (i) => { location.hash = regHref('assets', { 'f.asset_class': cls[i].label }); } });
    ch.status = await charts.doughnut(cStatus.querySelector('canvas'), d.status.map((s) => labelOf(s.label)), d.status.map((s) => s.n), { colors: stCols, onPick: (i) => { location.hash = regHref('assets', { 'f.asset_status': d.status[i].label }); } });
    ch.pm = await charts.barH(cPm.querySelector('canvas'), d.pm.map((r) => humanize(r.label)), [{ label: 'Completed', data: d.pm.map((r) => r.done), color: 2 }, { label: 'Completed late', data: d.pm.map((r) => r.stale), color: 4 }, { label: 'Scheduled', data: d.pm.map((r) => r.pending), color: 7 }],
      { stacked: true, onPick: (i) => { location.hash = regHref('assets', { 'f.asset_class': d.pm[i].label }); } });
    ch.loc = await charts.barH(cLoc.querySelector('canvas'), d.locations.map((r) => r.label), [{ label: 'Assets', data: d.locations.map((r) => r.n), color: 0 }], { onPick: (i) => { location.hash = regHref('assets', { 'f.location_code': d.locations[i].label }); } });
  }

  function tbl(heads, rows) {
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, heads.map((t, i) => h('th', { class: i ? 'right' : '' }, t)))), h('tbody', null, rows)));
  }

  function drawExtra(d) {
    const n = d.network;
    extra.replaceChildren(
      panel('Assets per engineer', { cls: 'span-7', flush: true, hint: 'workload, cover risk and PM still to do' }, tbl(['Engineer', 'Assets', 'Cover at risk', 'PM pending'], d.by_engineer.map((r) => h('tr', null,
        h('td', null, r.label === 'UNASSIGNED' ? h('span', { class: 'badge bad' }, icon('warning--alt--filled'), 'Unassigned') : entity('engineer', r.label, person(r.label), { mono: false })),
        h('td', { class: 'right' }, h('a', { href: regHref('assets', { 'f.engineer_name': r.label }) }, int(r.total))), h('td', { class: 'right' }, int(r.cover_risk)), h('td', { class: 'right' }, int(r.pm_pending)))))),
      panel('Network and users', { cls: 'span-5' }, h('div', { class: 'stack-v' },
        h('div', { class: 'kv' }, [['With an IP address', int(n.with_ip)], ['Network devices without an IP', int(n.missing_ip)], ['Not on the network', int(n.off_network)]].flatMap(([k, v]) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)])),
        barRows(d.hr.map((r) => ({ label: humanize(r.label), n: r.n, href: regHref('assets', { 'f.user_hr_status': r.label }) }))))),
      panel('Makes', { cls: 'span-4' }, barRows(d.by_make.map((r) => ({ label: r.label, n: r.n, href: regHref('assets', { 'f.make': r.label }) })))),
      panel('Installed by year', { cls: 'span-4', hint: 'age of the estate' }, barRows(d.age.map((r) => ({ label: r.label, n: r.n })))),
      panel('Cover ending, next 12 months', { cls: 'span-4', hint: 'renewal planning' }, d.expiry.length ? barRows(d.expiry.map((r) => ({ label: r.label, n: r.n, color: 'var(--chart-2)' }))) : h('div', { class: 'muted' }, 'No cover ends in the next 12 months.')),
      panel('Added and removed, last 90 days', { cls: 'span-6', flush: true }, d.changes.length ? tbl(['Date', 'Change', 'Assets'], d.changes.map((r) => h('tr', null, h('td', null, date(r.day)), h('td', { class: 'right' }, r.label === 'ADDED' ? 'Added' : 'Removed'), h('td', { class: 'right' }, int(r.n))))) : h('div', { class: 'muted', style: { padding: '16px' } }, 'No assets were added or removed in the last 90 days.')),
      panel('Cover type', { cls: 'span-6' }, barRows(d.cover_type.map((r) => ({ label: humanize(r.label), n: r.n, href: regHref('assets', { 'f.cover_type': r.label }) })))));
  }

  function attention(d) {
    if (!d.attention.length) return h('div', { class: 'empty' }, icon('checkmark--filled'), h('h3', null, 'Nothing needs attention'));
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, h('th', null, 'Asset'), h('th', null, 'Cover'), h('th', null, 'Ends'))),
      h('tbody', null, d.attention.map((r) => h('tr', null, h('td', null, h('a', { class: 'mono', href: regHref('assets', { open: r.id }) }, r.id), h('div', { class: 'faint', style: { fontSize: '12px' } }, `${humanize(r.asset_class)} · ${r.model || ''}`)), h('td', null, badge(r.cover_status)), h('td', null, date(r.cover_expiry_date)))))));
  }

  async function load(first) {
    try {
      const d = await get('/api/dash/assets');
      if (dead) return;
      if (first || !strip) { clear(body); await build(d); } else refreshLive(d);
      data = d;
    } catch (e) { if (!dead) { clear(body); body.append(h('div', { class: 'span-12' }, errorBlock(e, () => load(true)))); } }
  }

  function refreshLive(d) {
    kpiUpdate(strip, kpis(d));
    drawExtra(d);
    sub.textContent = `Register snapshot ${date(d.kpi.as_of)} · ${int(d.kpi.assets)} assets and ${int(d.kpi.components)} components · updated ${clock(Date.now())}`;
    const cls = d.by_class;
    charts.refresh(ch.class, cls.map((r) => humanize(r.label)), [cls.map((r) => r.in_use), cls.map((r) => r.idle), cls.map((r) => r.other)]);
    charts.refresh(ch.status, d.status.map((s) => labelOf(s.label)), [d.status.map((s) => s.n)]);
    charts.refresh(ch.pm, d.pm.map((r) => humanize(r.label)), [d.pm.map((r) => r.done), d.pm.map((r) => r.stale), d.pm.map((r) => r.pending)]);
    charts.refresh(ch.loc, d.locations.map((r) => r.label), [d.locations.map((r) => r.n)]);
  }

  load(true);
  return { onLive: (ev) => { if (!ev.tables.every((t) => NOISE.has(t))) load(false); }, destroy() { dead = true; Object.values(ch).forEach((c) => c?.destroy()); } };
}
