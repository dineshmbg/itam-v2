import { get } from '../core/api.js';
import { h, icon, clear } from '../core/dom.js';
import { date, dec, int, month, humanize, clock, person } from '../core/format.js';
import { badge, tickCross } from '../ui/badge.js';
import * as charts from '../ui/charts.js';
import { shareMenu } from '../ui/share.js';
import { entity } from '../ui/hovercard.js';
import { barRows, canvas, errorBlock, kpiStrip, kpiUpdate, legend, pageHead, panel, regHref, loading, NOISE } from './common.js';


export function mountCallsDash(root) {
  let strip, ch = {}, dead = false, extra;
  const sub = h('span', null, '');
  const body = h('div', { class: 'grid' });
  root.append(h('div', { class: 'page', 'data-mod': 'calls' }, pageHead('Call tracker', sub, shareMenu({ pack: 'calls' })), h('div', { id: 'kpis' }), body));
  body.append(loading());

  const kpis = (d) => {
    const k = d.kpi;
    return [
      { id: 'open', label: 'Calls raised, unresolved', value: int(k.open), sub: k.oldest_open != null ? `oldest ${int(k.oldest_open)} days` : 'none open', tone: k.open ? 'warn' : 'ok', ico: 'in-progress', href: regHref('calls', { 'f.call_status': 'OPEN' }) },
      { id: 'part', label: 'Waiting for a part', value: int(k.awaiting_part), sub: 'open, part not received', tone: k.awaiting_part ? 'warn' : 'ok', href: regHref('calls', { 'f.call_status': 'OPEN', 'f.spare_status': 'PART_PENDING' }) },
      { id: 'age', label: 'Median age of open calls', value: k.median_open_age != null ? dec(k.median_open_age) + ' d' : '—', sub: 'days since logged' },
      { id: 'tat', label: 'Average time to close', value: k.avg_tat != null ? dec(k.avg_tat) + ' d' : '—', sub: k.median_tat != null ? `median ${dec(k.median_tat)} d` : '' },
      { id: 'month', label: 'Calls this month', value: int(k.this_month), sub: `so far · last month ${int(k.last_month)}` },
      { id: 'rep', label: 'Repeat-failure assets', value: int(k.repeat_assets), sub: '3 or more calls', tone: k.repeat_assets ? 'warn' : 'ok' },
    ];
  };

  async function build(d) {
    strip = kpiStrip(kpis(d));
    root.querySelector('#kpis').replaceWith(strip);
    strip.id = 'kpis';
    sub.textContent = `Tracker snapshot ${date(d.kpi.as_of)} · ${int(d.kpi.total)} calls logged`;
    const m = d.monthly;
    const cMonth = canvas('Calls per month, closed and open, with average days to close', 'tall');
    const cAge = canvas('Open calls by age');
    const cProb = canvas('Most frequent problems', 'tall');
    const cClass = canvas('Calls by asset class');
    const cPri = canvas('Calls by priority', 'short');
    body.append(
      panel('Calls per month', { cls: 'span-8', hint: 'bars: calls · line: average days to close' }, cMonth,
        legend([{ label: 'Resolved', n: m.reduce((a, r) => a + r.closed, 0), color: 'var(--chart-3)' }, { label: 'Open', n: m.reduce((a, r) => a + r.open, 0), color: 'var(--chart-2)' }, { label: 'Average days to close', n: null, color: 'var(--c-text)' }])),
      panel('Open calls by age', { cls: 'span-4', hint: 'days since logged' }, cAge, h('div', { class: 'faint', style: { marginTop: '8px', fontSize: '12px' } }, 'Select a bar to list those calls.')),
      panel('Most frequent problems', { cls: 'span-6' }, cProb),
      panel('Asset class', { cls: 'span-3' }, cClass, legend(d.by_class.map((c, i) => ({ label: humanize(c.label), n: c.n, color: `var(--chart-${(i % 10) + 1})` })), (it, i) => regHref('calls', { 'f.asset_class': d.by_class[i].label }))),
      panel('Priority', { cls: 'span-3' }, cPri, legend(d.priority.map((p, i) => ({ label: p.label, n: p.n, color: `var(--chart-${(i % 8) + 1})` })), (it, i) => regHref('calls', { 'f.priority': d.priority[i].label }))),
      panel('Oldest open calls', { cls: 'span-8', flush: true, hint: 'select a row to open the call' }, oldest(d)),
      panel('Repeat failures', { cls: 'span-4', hint: 'assets with 3+ calls' }, d.repeat.length ? barRows(d.repeat.map((r) => ({ label: r.id, n: r.n, href: regHref('calls', { q: r.id }) }))) : h('div', { class: 'muted' }, 'No repeat failures.')),
      panel('Parts flow', { cls: 'span-6', hint: 'received from vendor · faulty parts sent back' }, flow(d)),
      panel('OEM RMA (Cisco / Juniper)', { cls: 'span-6' }, rma(d)),
    );
    extra = h('div', { class: 'contents' });
    body.append(extra);
    drawExtra(d);
    ch.month = await charts.monthly(cMonth.querySelector('canvas'), m.map((r) => month(r.label)),
      [{ label: 'Resolved', data: m.map((r) => r.closed), color: 2 }, { label: 'Open', data: m.map((r) => r.open), color: 1 }], { label: 'Average days to close', data: m.map((r) => r.avg_tat) },
      { onPick: (i) => { location.hash = regHref('calls', { 'f.month': m[i].label }); } });
    ch.age = await charts.barH(cAge.querySelector('canvas'), d.ageing.map((r) => r.label), [{ label: 'Open calls', data: d.ageing.map((r) => r.n), color: 1 }],
      { colors: [2, 4, 1, 1, 7], onPick: (i) => { location.hash = regHref('calls', { 'f.call_status': 'OPEN', 'f.age_bucket': d.ageing[i].label }); } });
    ch.prob = await charts.barH(cProb.querySelector('canvas'), d.problems.map((r) => r.label.length > 34 ? r.label.slice(0, 33) + '…' : r.label), [{ label: 'Calls', data: d.problems.map((r) => r.n), color: 0 }],
      { onPick: (i) => { location.hash = regHref('calls', { q: d.problems[i].label }); } });
    ch.cls = await charts.doughnut(cClass.querySelector('canvas'), d.by_class.map((c) => humanize(c.label)), d.by_class.map((c) => c.n), { onPick: (i) => { location.hash = regHref('calls', { 'f.asset_class': d.by_class[i].label }); } });
    ch.pri = await charts.barH(cPri.querySelector('canvas'), d.priority.map((p) => p.label), [{ label: 'Calls', data: d.priority.map((p) => p.n), color: 0 }], { colors: [7, 0, 6] });
  }

  function tbl(heads, rows) {
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, heads.map((t, i) => h('th', { class: i ? 'right' : '' }, t)))), h('tbody', null, rows)));
  }

  function drawExtra(d) {
    const s = d.sla;
    const pctOf = (n) => (s.closed ? Math.round(1000 * n / s.closed) / 10 : 0);
    const meter = (label, n) => h('div', { class: 'bar-row', style: { gridTemplateColumns: 'minmax(96px, 30%) 1fr auto' } }, h('span', { class: 'bar-l' }, label), h('span', { class: 'bar-t' }, h('i', { style: { width: pctOf(n) + '%', background: 'var(--chart-3)' } })), h('span', { class: 'bar-n' }, `${dec(pctOf(n))}%`));
    extra.replaceChildren(
      panel('By engineer', { cls: 'span-7', flush: true, hint: 'workload and turnaround' }, tbl(['Engineer', 'Calls', 'Open', 'Closed', 'Avg TAT (d)', 'Oldest open (d)'], d.by_engineer.map((r) => h('tr', null,
        h('td', null, r.label === 'UNASSIGNED' ? h('span', { class: 'badge bad' }, icon('warning--alt--filled'), 'Unassigned') : entity('engineer', r.label, person(r.label), { mono: false })),
        h('td', { class: 'right' }, int(r.total)), h('td', { class: 'right' }, r.open ? h('a', { href: regHref('calls', { 'f.call_status': 'OPEN', 'f.engineer': r.label }) }, int(r.open)) : '0'), h('td', { class: 'right' }, int(r.closed)),
        h('td', { class: 'right' }, r.avg_tat != null ? dec(r.avg_tat) : '—'), h('td', { class: 'right' }, r.oldest != null ? int(r.oldest) : '—'))))),
      panel('Closed within', { cls: 'span-5', hint: `${int(s.closed)} closed calls` }, h('div', { class: 'stack-v' }, meter('1 day', s.within_1d), meter('3 days', s.within_3d), meter('7 days', s.within_7d),
        h('div', { class: 'muted small' }, `Average days from ONGC logging to CIPL logging: ${d.logging_lag.avg_lag != null ? dec(d.logging_lag.avg_lag) : '—'} · ${int(d.logging_lag.slow)} calls logged 5+ days late`))),
      panel('Turnaround by asset class', { cls: 'span-6', flush: true }, tbl(['Class', 'Closed', 'Open', 'Avg TAT (d)'], d.tat_class.map((r) => h('tr', null, h('td', null, humanize(r.label)), h('td', { class: 'right' }, int(r.closed)), h('td', { class: 'right' }, int(r.open)), h('td', { class: 'right' }, r.avg_tat != null ? dec(r.avg_tat) : '—'))))),
      panel('Calls by site', { cls: 'span-3' }, barRows(d.by_site.map((r) => ({ label: r.label, n: r.n, href: regHref('calls', { q: r.label }) })))),
      panel('Last 30 days', { cls: 'span-3', hint: 'busiest recent days' }, barRows(d.daily.filter((r) => r.opened || r.closed).slice(-10).reverse().map((r) => ({ label: `${date(r.day)}  +${r.opened} / -${r.closed}`, n: r.opened + r.closed })))));
  }

  function oldest(d) {
    if (!d.oldest.length) return h('div', { class: 'empty' }, icon('checkmark--filled'), h('h3', null, 'No open calls'));
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['SR ID', 'Logged', 'Asset', 'Problem', 'Engineer', 'Age', 'Spare'].map((t, i) => h('th', { class: i === 5 ? 'right' : '' }, t)))),
      h('tbody', null, d.oldest.map((r) => h('tr', null,
        h('td', null, h('a', { class: 'mono', href: regHref('calls', { open: r.id }) }, r.id)), h('td', null, date(r.cipl_call_date)), h('td', null, h('span', { class: 'mono' }, r.asset_key || '—')),
        h('td', { class: 'wrap' }, r.problem_description || '—'), h('td', null, person(r.engineer)), h('td', { class: 'right' }, `${int(r.ageing_days)} d`), h('td', null, badge(r.spare_status)))))));
  }

  function flow(d) {
    const s = d.spares;
    const row = (ok, text, n, href) => h('div', { class: 'row', style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '6px 0', borderBottom: '1px solid var(--c-border)' } },
      tickCross(ok, 'OK', 'Needs attention'), h('span', { style: { flex: 1 } }, text), href ? h('a', { href }, int(n)) : h('strong', null, int(n)));
    return h('div', null,
      row(true, 'Inward lines (parts received)', s.inward, regHref('inward')),
      row(s.inward_pending === 0, 'Received lines with no received date', s.inward_pending, regHref('inward', { 'f.receipt': 'Pending' })),
      h('div', { class: 'row', style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '6px 0', borderBottom: '1px solid var(--c-border)' } },
        tickCross(true, 'OK', 'Needs attention'), h('span', { style: { flex: 1 } }, 'Average days from inward entry to receipt'), h('strong', null, s.avg_transit_days != null ? `${dec(s.avg_transit_days)} d` : '—')),
      row(true, 'Outward lines (faulty parts sent)', s.outward, regHref('outward')),
      row(s.outward_no_gatepass === 0, 'Sent lines with no gate pass', s.outward_no_gatepass, regHref('outward', { 'f.gatepass': 'Missing' })),
      row(s.outward_not_sent === 0, 'Outward lines not yet sent', s.outward_not_sent, regHref('outward', { 'f.dispatch': 'Not sent' })),
      s.top_parts.length ? h('div', { style: { marginTop: '10px' } },
        h('div', { class: 'muted small', style: { marginBottom: '6px' } }, 'Most requested parts'),
        barRows(s.top_parts.map((p) => ({ label: p.label.length > 40 ? p.label.slice(0, 39) + '…' : p.label, n: p.n, href: regHref('inward', { q: p.label }) })))) : null);
  }

  function rma(d) {
    const r = d.rma;
    return h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '16px' } },
      stat('RMA lines', int(r.total), regHref('rma')), stat('Awaiting faulty-part return', int(r.pending_return), regHref('rma', { 'f.return_status': 'PENDING' }), r.pending_return ? 'warn' : 'ok'),
      stat('Average days to replacement', r.avg_days_to_replace != null ? dec(r.avg_days_to_replace) : '—'),
      h('div', { style: { gridColumn: '1 / -1' } }, h('div', { class: 'muted', style: { marginBottom: '6px', fontSize: '12px' } }, 'Lines per year'), barRows(r.by_year.map((y) => ({ label: y.label, n: y.n, href: regHref('rma', { 'f.year': y.label }) })))));
  }
  const stat = (l, v, href, tone) => h(href ? 'a' : 'div', { class: 'kpi', href: href || null, 'data-tone': tone || null, style: { border: '1px solid var(--c-border)', textDecoration: 'none' } }, h('span', { class: 'kpi-l' }, l), h('span', { class: 'kpi-v' }, v));

  async function load(first) {
    try {
      const d = await get('/api/dash/calls');
      if (dead) return;
      if (first || !strip) { clear(body); await build(d); return; }
      kpiUpdate(strip, kpis(d));
      drawExtra(d);
      sub.textContent = `Tracker snapshot ${date(d.kpi.as_of)} · ${int(d.kpi.total)} calls logged · updated ${clock(Date.now())}`;
      charts.refresh(ch.month, d.monthly.map((r) => month(r.label)), [d.monthly.map((r) => r.closed), d.monthly.map((r) => r.open), d.monthly.map((r) => r.avg_tat)]);
      charts.refresh(ch.age, d.ageing.map((r) => r.label), [d.ageing.map((r) => r.n)]);
      charts.refresh(ch.prob, d.problems.map((r) => (r.label.length > 34 ? r.label.slice(0, 33) + '…' : r.label)), [d.problems.map((r) => r.n)]);
      charts.refresh(ch.cls, d.by_class.map((c) => humanize(c.label)), [d.by_class.map((c) => c.n)]);
    } catch (e) { if (!dead) { clear(body); body.append(h('div', { class: 'span-12' }, errorBlock(e, () => load(true)))); } }
  }
  load(true);
  return { onLive: (ev) => { if (!ev.tables.every((t) => NOISE.has(t))) load(false); }, destroy() { dead = true; Object.values(ch).forEach((c) => c?.destroy()); } };
}
