// Preventive maintenance dashboard: where the quarter stands, who owes what, how it compares with earlier quarters. Live.
import { get } from '../core/api.js';
import { h, icon, clear } from '../core/dom.js';
import { date, int, pct, clock, person } from '../core/format.js';
import * as charts from '../ui/charts.js';
import { shareMenu } from '../ui/share.js';
import { entity } from '../ui/hovercard.js';
import { barRows, canvas, errorBlock, kpiStrip, kpiUpdate, legend, pageHead, panel, regHref, loading } from './common.js';

export function mountPmDash(root) {
  let strip, ch = {}, dead = false, timeline;
  const sub = h('span', null, '');
  const body = h('div', { class: 'grid' });
  const tools = [h('a', { class: 'btn', href: '#/registers/pm' }, icon('data-table'), 'Worklist'), h('a', { class: 'btn', href: '#/pm/cycles' }, icon('calendar'), 'Cycles and snapshots'), shareMenu({ pack: 'pm' })];
  root.append(h('div', { class: 'page', 'data-mod': 'pm' }, pageHead('Preventive maintenance', sub, tools), h('div', { id: 'kpis' }), body));
  body.append(loading());

  const kpis = (d) => {
    const k = d.kpi;
    const behind = k.pct_done != null && k.pct_done + 5 < k.expected_pct;
    return [
      { id: 'scope', label: 'Assets in scope', value: int(k.scope), sub: 'desktops, workstations, servers, network, printers…', ico: 'devices', href: regHref('pm', { 'f.pm_status': '' }) },
      { id: 'done', label: 'PM done', value: int(k.done), sub: k.pct_done != null ? `${pct(k.pct_done)} of scope` : '', tone: 'ok', href: regHref('pm', { 'f.pm_status': 'DONE' }) },
      { id: 'pending', label: 'PM pending', value: int(k.pending), sub: d.cycle.overdue ? 'quarter ended, not closed yet' : `${int(k.days_left)} days left in the quarter`, tone: k.pending ? (behind ? 'bad' : 'warn') : 'ok', href: regHref('pm', { 'f.pm_status': 'PENDING' }) },
      { id: 'stale', label: 'Done outside quarter', value: int(k.stale), sub: 'dated before this quarter', tone: k.stale ? 'warn' : 'ok', href: regHref('pm', { 'f.pm_status': 'DONE_OUTSIDE_QUARTER' }) },
      { id: 'pace', label: 'Time elapsed', value: pct(k.expected_pct), sub: behind ? 'work is behind the calendar' : 'work is on pace', tone: behind ? 'bad' : 'ok' },
      { id: 'un', label: 'Pending, no engineer', value: int(k.unassigned), sub: 'nobody is responsible yet', tone: k.unassigned ? 'bad' : 'ok', href: regHref('pm', { 'f.pm_status': 'PENDING', 'f.engineer_name': '~blank' }) },
    ];
  };

  function progress(d) {
    const k = d.kpi, c = d.cycle;
    const kick = Math.max(0, Math.min(100, 100 * (new Date(c.kickoff) - new Date(c.start)) / (new Date(c.end) - new Date(c.start))));
    const kickTxt = k.kickoff_in_days > 0 ? `kick-off in ${k.kickoff_in_days} days (${date(c.kickoff)})` : `kick-off was ${date(c.kickoff)}${c.kickoff_sent ? ' · e-mail sent' : ''}`;
    return h('div', { class: 'cycle-bar', role: 'img', 'aria-label': `${pct(k.pct_done)} of assets done, ${pct(k.expected_pct)} of the quarter elapsed` },
      h('div', { class: 'cb-h' }, h('strong', null, c.label), h('span', { class: 'muted' }, `${date(c.start)} → ${date(c.end)} · ${kickTxt}`), c.overdue ? h('span', { class: 'badge bad' }, icon('warning--alt--filled'), 'Not closed yet') : h('span', { class: 'badge ' + (c.status === 'OPEN' ? 'info' : 'mute') }, c.status === 'OPEN' ? 'Open' : 'Closed')),
      c.overdue ? h('div', { class: 'cb-over' }, `These are still the ${c.label} figures. That quarter ended ${date(c.end)} but has not been closed, so ${c.calendar_label} has not started counting. `,
        h('a', { href: '#/pm/cycles' }, 'Close it under Cycles and snapshots'), ' to reset every asset to pending.') : null,
      h('div', { class: 'cb-track' }, h('i', { class: 'cb-time', style: { width: k.expected_pct + '%' } }), h('i', { class: 'cb-done', style: { width: (k.pct_done || 0) + '%' } }), h('i', { class: 'cb-kick', style: { left: kick + '%' }, title: 'Kick-off' })),
      h('div', { class: 'cb-l' }, h('span', null, h('i', { class: 'sw', style: { background: 'var(--chart-3)' } }), `Done ${pct(k.pct_done)}`), h('span', null, h('i', { class: 'sw', style: { background: 'var(--c-border-strong)' } }), `Time elapsed ${pct(k.expected_pct)}`)));
  }

  function engTable(d) {
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['Engineer', 'In scope', 'Done', 'Outside quarter', 'Pending', 'Progress'].map((t, i) => h('th', { class: i && i < 5 ? 'right' : '' }, t)))),
      h('tbody', null, d.by_engineer.map((r) => {
        const p = r.scope ? 100 * r.done / r.scope : 0;
        return h('tr', null,
          h('td', null, r.label === 'UNASSIGNED' ? h('span', { class: 'badge bad' }, icon('warning--alt--filled'), 'Unassigned') : entity('engineer', r.label, person(r.label), { mono: false })),
          h('td', { class: 'right' }, int(r.scope)), h('td', { class: 'right' }, h('a', { href: regHref('pm', { 'f.pm_status': 'DONE', 'f.engineer_name': r.label }) }, int(r.done))), h('td', { class: 'right' }, int(r.stale)),
          h('td', { class: 'right' }, r.pending ? h('a', { href: regHref('pm', { 'f.pm_status': 'PENDING', 'f.engineer_name': r.label }) }, int(r.pending)) : '0'),
          h('td', null, h('span', { class: 'meter' }, h('span', { class: 'bar-t' }, h('i', { style: { width: p + '%', background: 'var(--chart-3)' } })), h('span', null, pct(Math.round(p * 10) / 10)))));
      }))));
  }

  function drawExtra(d) {
    const rows = d.quarters;
    timeline.replaceChildren(
      panel('Progress by engineer', { cls: 'span-8', flush: true, hint: 'select a number to open that worklist' }, engTable(d)),
      panel('Pending by location', { cls: 'span-4' }, d.by_location.some((r) => r.pending) ? barRows(d.by_location.filter((r) => r.pending).map((r) => ({ label: r.label, n: r.pending, color: 'var(--chart-8)', href: regHref('pm', { 'f.pm_status': 'PENDING', 'f.location_code': r.label }) }))) : h('div', { class: 'muted' }, 'Nothing pending.')),
      panel('Signed off by', { cls: 'span-4', hint: 'who accepted the completed PM' }, barRows(d.signers.map((r) => ({ label: r.label, n: r.n })))),
      panel('Earlier quarters', { cls: 'span-4', hint: 'frozen snapshots' }, rows.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Quarter', 'As of', 'Done', 'Pending'].map((t) => h('th', null, t)))),
        h('tbody', null, rows.map((r) => h('tr', null, h('td', null, r.label), h('td', null, date(r.as_of)), h('td', null, `${int(r.done)} / ${int(r.scope)}`), h('td', null, int(r.pending))))))) : h('div', { class: 'muted' }, 'No quarter has been closed yet. A snapshot is frozen when a quarter is closed (Cycles and snapshots).')),
      panel('Recently recorded', { cls: 'span-4', flush: true }, d.recent.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Asset', 'Date', 'By'].map((t) => h('th', null, t)))),
        h('tbody', null, d.recent.map((r) => h('tr', null, h('td', null, entity('asset', r.asset_key, r.asset_key)), h('td', null, date(r.pm_date)), h('td', null, person(r.recorded_by || ''))))))) : h('div', { class: 'muted', style: { padding: '16px' } }, 'Nothing recorded from the portal yet.')),
    );
  }

  async function build(d) {
    strip = kpiStrip(kpis(d));
    root.querySelector('#kpis').replaceWith(strip);
    strip.id = 'kpis';
    sub.textContent = `${d.cycle.label} · ${int(d.kpi.scope)} assets in scope`;
    const cEng = canvas('Preventive maintenance per engineer', 'tall');
    const cClass = canvas('Preventive maintenance per asset class', 'tall');
    const cBurn = canvas('PM completed per day, with running total', 'tall');
    timeline = h('div', { class: 'contents' });
    body.append(h('div', { class: 'span-12' }, progress(d)),
      panel('Completion per day', { cls: 'span-8', hint: 'bars: PM dated that day · line: running total' }, cBurn),
      panel('By asset class', { cls: 'span-4' }, cClass, legend([{ label: 'Done', color: 'var(--chart-3)' }, { label: 'Done outside quarter', color: 'var(--chart-5)' }, { label: 'Pending', color: 'var(--chart-8)' }])),
      panel('By engineer', { cls: 'span-12' }, cEng), timeline);
    drawExtra(d);
    const days = d.burn.map((r) => date(r.day)); let run = 0;
    const cum = d.burn.map((r) => (run += r.n));
    ch.burn = await charts.monthly(cBurn.querySelector('canvas'), days, [{ label: 'PM done that day', data: d.burn.map((r) => r.n), color: 2 }], { label: 'Running total', data: cum });
    const e = d.by_engineer;
    ch.eng = await charts.barH(cEng.querySelector('canvas'), e.map((r) => person(r.label)), [{ label: 'Done', data: e.map((r) => r.done), color: 2 }, { label: 'Outside quarter', data: e.map((r) => r.stale), color: 4 }, { label: 'Pending', data: e.map((r) => r.pending), color: 7 }],
      { stacked: true, onPick: (i) => { location.hash = regHref('pm', { 'f.engineer_name': e[i].label, 'f.pm_status': '' }); } });
    const c = d.by_class;
    ch.cls = await charts.barH(cClass.querySelector('canvas'), c.map((r) => r.label), [{ label: 'Done', data: c.map((r) => r.done), color: 2 }, { label: 'Outside', data: c.map((r) => r.stale), color: 4 }, { label: 'Pending', data: c.map((r) => r.pending), color: 7 }],
      { stacked: true, onPick: (i) => { location.hash = regHref('pm', { 'f.asset_class': c[i].label, 'f.pm_status': '' }); } });
  }

  async function load(first) {
    try {
      const d = await get('/api/pm/dashboard');
      if (dead) return;
      if (first || !strip) { clear(body); await build(d); return; }
      kpiUpdate(strip, kpis(d));
      sub.textContent = `${d.cycle.label} · ${int(d.kpi.scope)} assets in scope · updated ${clock(Date.now())}`;
      body.querySelector('.cycle-bar')?.replaceWith(progress(d));
      drawExtra(d);
      const e = d.by_engineer, c = d.by_class;
      charts.refresh(ch.eng, e.map((r) => person(r.label)), [e.map((r) => r.done), e.map((r) => r.stale), e.map((r) => r.pending)]);
      charts.refresh(ch.cls, c.map((r) => r.label), [c.map((r) => r.done), c.map((r) => r.stale), c.map((r) => r.pending)]);
      let run = 0;
      charts.refresh(ch.burn, d.burn.map((r) => date(r.day)), [d.burn.map((r) => r.n), d.burn.map((r) => (run += r.n))]);
    } catch (e) { if (!dead) { clear(body); body.append(h('div', { class: 'span-12' }, errorBlock(e, () => load(true)))); } }
  }
  load(true);
  return { onLive: (ev) => { if (ev.tables.some((t) => ['asset', 'pm_record', 'pm_cycle'].includes(t))) load(false); }, destroy() { dead = true; Object.values(ch).forEach((c) => c?.destroy()); } };
}
