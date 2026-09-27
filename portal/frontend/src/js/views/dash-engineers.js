import { get } from '../core/api.js';
import { h, icon, clear, debounce } from '../core/dom.js';
import { date, dec, int, pct, humanize, clock } from '../core/format.js';
import { current, patchParam, href as routeHref } from '../core/router.js';
import { badge, genderGlyph, tickCross } from '../ui/badge.js';
import * as charts from '../ui/charts.js';
import { openDrawer } from '../ui/drawer.js';
import { shareMenu } from '../ui/share.js';
import { send } from '../core/api.js';
import { toast } from '../core/editor.js';
import { isAdmin } from '../core/session.js';
import { openModal } from '../ui/modal.js';
import { barRows, canvas, errorBlock, kpiStrip, kpiUpdate, pageHead, panel, regHref, loading } from './common.js';

const ITEM_LABEL = { JOINING_KIT: 'Joining kit', ID_CARD: 'ID card', ONSURITY: 'Onsurity', MEDICAL_ESIC: 'Medical / ESIC', POLICE_VERIFICATION: 'Police verification', SALARY_ACCOUNT: 'Salary account', JACKETS: 'Jackets', ONGC_GATEPASS: 'ONGC gate pass' };

const NOISE = new Set(['portal_activity', 'portal_session', 'portal_backup', 'notify_log', 'portal_import']);   // bookkeeping tables: nothing on a dashboard depends on them

export function mountEngineersDash(root) {
  let strip, ch = {}, dead = false, data, drawer;
  const filters = { q: '', open: false, roster: false, onboarding: false };
  const sub = h('span', null, '');
  const body = h('div', { class: 'grid' });
  root.append(h('div', { class: 'page' }, pageHead('Engineers', sub, shareMenu({ pack: 'engineers' })), h('div', { id: 'kpis' }), body));
  body.append(loading());

  const kpis = (d) => {
    const k = d.kpi;
    return [
      { id: 'eng', label: 'CIPL engineers on roster', value: int(k.engineers), sub: `${int(k.with_workload)} with assigned work`, ico: 'user--multiple' },
      { id: 'open', label: 'Open calls', value: int(k.open_calls), sub: 'across all engineers', tone: k.open_calls ? 'warn' : 'ok', href: regHref('calls', { 'f.call_status': 'OPEN' }) },
      { id: 'assets', label: 'Assets under care', value: int(k.assets), sub: 'engineer responsible', href: regHref('assets') },
      { id: 'pmpct', label: 'PM completion', value: pct(k.pm_pct), sub: 'this quarter, tracked assets', tone: k.pm_pct >= 95 ? 'ok' : 'warn' },
      { id: 'pmp', label: 'PM pending', value: int(k.pm_pending), sub: 'assets awaiting maintenance', tone: k.pm_pending ? 'warn' : 'ok', href: regHref('assets', { 'f.pm_status': 'PENDING' }) },
      { id: 'onb', label: 'Onboarding open', value: int(k.onboarding_open), sub: 'joining formalities pending', tone: k.onboarding_open ? 'warn' : 'ok' },
    ];
  };

  const visible = () => {
    const q = filters.q.trim().toLowerCase();
    return data.rows.filter((r) => (!q || `${r.display_name} ${r.ecode || ''} ${r.designation || ''}`.toLowerCase().includes(q))
      && (!filters.open || r.calls_open > 0) && (!filters.roster || r.on_roster) && (!filters.onboarding || (r.onboarding_status && r.onboarding_status !== 'COMPLETE')));
  };

  const tableBox = h('div', { class: 'tbl-wrap' });
  const countEl = h('span', { class: 'count-line' }, '');
  const search = h('input', { type: 'search', placeholder: 'Filter engineers by name, code or role', 'aria-label': 'Filter engineers' });
  const pill = (key, label) => {
    const input = h('input', { type: 'checkbox' });
    input.addEventListener('change', () => { filters[key] = input.checked; drawTable(); });
    return h('label', { class: 'pill-toggle' }, input, icon('checkbox', 'off'), icon('checkbox--checked--filled', 'on'), label);
  };
  search.addEventListener('input', debounce(() => { filters.q = search.value; drawTable(); }, 90));
  const toolbar = h('div', { class: 'toolbar' }, h('div', { class: 'field' }, icon('search'), search), pill('open', 'Has open calls'), pill('roster', 'On CIPL roster'), pill('onboarding', 'Onboarding incomplete'), countEl);

  function drawTable() {
    const rows = visible();
    countEl.replaceChildren(h('strong', null, int(rows.length)), ` of ${int(data.rows.length)} engineers`);
    tableBox.replaceChildren(rows.length ? h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['Engineer', 'Level', 'Assets', 'PM done', 'Calls', 'Avg close', 'Oldest open', 'Onboarding'].map((t, i) => h('th', { class: [2, 5, 6].includes(i) ? 'right' : '' }, t)))),
      h('tbody', null, rows.map((r) => {
        const tr = h('tr', { tabindex: '0', style: { cursor: 'pointer' }, 'data-id': r.id },
          h('td', null, h('div', { class: 'eng-cell' }, genderGlyph(r.gender), h('div', null, h('div', { class: 'name' }, r.display_name), h('div', { class: 'sub' }, [r.designation || (r.on_roster ? '' : 'Not on CIPL roster'), r.ecode].filter(Boolean).join(' · '))))),
          h('td', null, r.level || '—'), h('td', { class: 'right' }, int(r.assets)),
          h('td', null, r.pm_pct == null ? h('span', { class: 'faint' }, '—') : h('span', { class: 'meter' }, h('span', { class: 'bar-t' }, h('i', { style: { width: r.pm_pct + '%', background: r.pm_pct >= 95 ? 'var(--c-ok)' : 'var(--c-warn)' } })), pct(r.pm_pct))),
          h('td', null, r.calls_total ? h('span', { class: 'meter' }, h('span', { class: 'stack', style: { width: '96px' }, title: `${r.calls_open} open, ${r.calls_closed} closed` }, h('i', { style: { width: (100 * r.calls_closed / r.calls_total) + '%', background: 'var(--chart-3)' } }), h('i', { style: { width: (100 * r.calls_open / r.calls_total) + '%', background: 'var(--chart-2)' } })), `${int(r.calls_open)} open / ${int(r.calls_total)}`) : h('span', { class: 'faint' }, '—')),
          h('td', { class: 'right' }, r.avg_tat != null ? `${dec(r.avg_tat)} d` : '—'), h('td', { class: 'right' }, r.oldest_open != null ? `${int(r.oldest_open)} d` : '—'),
          h('td', null, r.onboarding_status ? badge(r.onboarding_status) : h('span', { class: 'faint' }, '—')));
        const open = () => { history.replaceState(null, '', routeHref('dashboard/engineers', { eng: r.id })); showEngineer(r.id); };
        tr.addEventListener('click', open);
        tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
        return tr;
      }))) : h('div', { class: 'empty' }, icon('search'), h('h3', null, 'No engineers match'), h('div', null, 'Adjust the filters above.')));
  }

  async function build(d) {
    data = d;
    strip = kpiStrip(kpis(d));
    root.querySelector('#kpis').replaceWith(strip);
    strip.id = 'kpis';
    sub.textContent = `Roster snapshot ${date(d.kpi.as_of)} · workload from the current asset register and call tracker`;
    const work = d.rows.filter((r) => r.calls_total || r.assets).slice(0, 14);
    const cWork = canvas('Calls per engineer, open and closed', 'tall');
    const cAssets = canvas('Assets per engineer', 'tall');
    body.append(
      panel('Call workload', { cls: 'span-7', hint: 'select a bar for the engineer’s detail' }, cWork),
      panel('Assets under care', { cls: 'span-5' }, cAssets),
      panel('Engineers', { cls: 'span-12', flush: true, hint: 'select a row for calls, assets and onboarding' }, toolbar, tableBox),
      panel('Onboarding status of the roster', { cls: 'span-4' }, barRows(d.onboarding.map((o) => ({ label: humanize(o.label), n: o.n, color: o.label === 'COMPLETE' ? 'var(--c-ok)' : 'var(--c-warn)' })))),
    );
    drawTable();
    const names = work.map((r) => r.display_name);
    const byCalls = work.filter((r) => r.calls_total).sort((a, b) => b.calls_total - a.calls_total);
    ch.work = await charts.barH(cWork.querySelector('canvas'), byCalls.map((r) => r.display_name), [{ label: 'Closed', data: byCalls.map((r) => r.calls_closed), color: 2 }, { label: 'Open', data: byCalls.map((r) => r.calls_open), color: 1 }],
      { stacked: true, onPick: (i) => showEngineer(byCalls[i].id) });
    const byAssets = work.filter((r) => r.assets).sort((a, b) => b.assets - a.assets);
    ch.assets = await charts.barH(cAssets.querySelector('canvas'), byAssets.map((r) => r.display_name), [{ label: 'Assets', data: byAssets.map((r) => r.assets), color: 0 }], { onPick: (i) => showEngineer(byAssets[i].id) });
    void names;
    const eng = current().params.get('eng');
    if (eng) showEngineer(eng);
  }

  async function showEngineer(key) {
    drawer?.close(true);
    drawer = openDrawer({ title: 'Engineer', subtitle: 'Loading…', onClose: () => patchParam('eng', null) });
    try {
      const e = await get('/api/engineers/' + encodeURIComponent(key));
      drawer.setTitle(e.display_name, [e.designation, e.ecode].filter(Boolean).join(' · ') || 'Not on the CIPL roster');
      const b = drawer.body;
      b.replaceChildren(
        isAdmin() && e.ecode ? h('div', { class: 'dsec' }, h('button', { class: 'btn', type: 'button', onClick: () => rosterEvent(e) }, icon('user'), e.employment_status === 'ACTIVE' ? 'Record resignation / transfer…' : 'Record rejoining…')) : null,
        h('div', { class: 'dsec' }, h('div', { class: 'kv' },
          kv('Gender', genderGlyph(e.gender)), kv('Level', e.level || '—'), kv('Roster status', e.employment_status ? badge(e.employment_status === 'ACTIVE' ? 'ACTIVE' : e.employment_status) : h('span', { class: 'faint' }, 'Not on roster')),
          kv('Joined ONGC site', date(e.date_of_joining_ongc)), kv('Skill category', e.skill_category || '—'), kv('Deployed at', humanize(e.deployed_at)), kv('Work email', e.company_email || '—'),
          ...('mobile_no' in e ? kv('Mobile', e.mobile_no || '—') : []), ...('personal_email' in e ? kv('Personal e-mail', e.personal_email || '—') : []))),
        e.checklist.length ? h('div', { class: 'dsec' }, h('h3', null, 'Onboarding checklist'), h('div', { class: 'checklist' }, e.checklist.map((c) => h('div', { class: 'row' }, tickCross(c.status !== 'PENDING', 'Complete', 'Pending'), ITEM_LABEL[c.item] || humanize(c.item))))) : null,
        h('div', { class: 'dsec' }, h('h3', null, `Open calls (${e.open_calls.length})`), e.open_calls.length ? miniTable(e.open_calls, ['id', 'cipl_call_date', 'problem_description', 'ageing_days'], ['SR ID', 'Logged', 'Problem', 'Age (d)'], 'calls') : h('div', { class: 'muted' }, 'No open calls.')),
        h('div', { class: 'dsec' }, h('h3', null, 'Recently closed'), e.recent_closed.length ? miniTable(e.recent_closed, ['id', 'closed_date', 'problem_description', 'tat_days'], ['SR ID', 'Closed', 'Problem', 'Days'], 'calls') : h('div', { class: 'muted' }, 'None.')),
        h('div', { class: 'dsec' }, h('h3', null, 'Assets by class'), e.assets_by_class.length ? barRows(e.assets_by_class.map((a) => ({ label: `${humanize(a.label)}${a.pm_pending ? ` · ${a.pm_pending} PM pending` : ''}`, n: a.n, href: regHref('assets', { 'f.asset_class': a.label, 'f.engineer_name': e.id }) }))) : h('div', { class: 'muted' }, 'No assets assigned.')),
      );
    } catch (err) { drawer.body.replaceChildren(errorBlock(err)); }
  }
  const kv = (k, v) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)];

  function rosterEvent(e) {
    const active = e.employment_status === 'ACTIVE';
    const type = h('select', { id: 'ev-type' }, (active ? [['RESIGNED', 'Resigned'], ['TERMINATED', 'Terminated'], ['TRANSFERRED', 'Transferred']] : [['REJOINED', 'Rejoined']]).map(([v, t]) => h('option', { value: v }, t)));
    const day = h('input', { id: 'ev-date', type: 'date', value: new Date().toISOString().slice(0, 10) });
    const why = h('input', { id: 'ev-why', type: 'text', maxlength: '200', placeholder: 'Optional' });
    const f = (id, label, el) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), el);
    openModal({ title: `Roster event · ${e.display_name}`, lead: 'Recorded in the CIPL lifecycle log and applied to the roster at once.',
      body: h('div', null, f('ev-type', 'Event', type), f('ev-date', 'Effective date', day), f('ev-why', 'Reason', why)),
      actions: [{ label: 'Cancel' }, { label: 'Record', primary: true, onClick: async () => {
        await send(`/api/engineers/${encodeURIComponent(e.id)}/event`, { type: type.value, date: day.value, reason: why.value });
        toast('Roster updated'); showEngineer(e.id); load(false);
      } }] });
  }
  function miniTable(rows, keys, heads, ds) {
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, heads.map((t) => h('th', null, t)))),
      h('tbody', null, rows.map((r) => h('tr', null, keys.map((k, i) => h('td', { class: i === 2 ? 'wrap' : '' }, i === 0 ? h('a', { class: 'mono', href: regHref(ds, { open: r[k] }) }, r[k]) : k.includes('date') ? date(r[k]) : (r[k] ?? '—')))))))
    );
  }

  async function load(first) {
    try {
      const d = await get('/api/dash/engineers');
      if (dead) return;
      if (first || !strip) { clear(body); await build(d); return; }
      data = d;
      kpiUpdate(strip, kpis(d));
      sub.textContent = `Roster snapshot ${date(d.kpi.as_of)} · updated ${clock(Date.now())}`;
      drawTable();
      const work = d.rows.filter((r) => r.calls_total || r.assets);
      const byCalls = work.filter((r) => r.calls_total).sort((a, b) => b.calls_total - a.calls_total).slice(0, 14);
      charts.refresh(ch.work, byCalls.map((r) => r.display_name), [byCalls.map((r) => r.calls_closed), byCalls.map((r) => r.calls_open)]);
      const byAssets = work.filter((r) => r.assets).sort((a, b) => b.assets - a.assets).slice(0, 14);
      charts.refresh(ch.assets, byAssets.map((r) => r.display_name), [byAssets.map((r) => r.assets)]);
    } catch (e) { if (!dead) { clear(body); body.append(h('div', { class: 'span-12' }, errorBlock(e, () => load(true)))); } }
  }
  load(true);
  return { onLive: (ev) => { if (!ev.tables.every((t) => NOISE.has(t))) load(false); }, destroy() { dead = true; drawer?.close(true); Object.values(ch).forEach((c) => c?.destroy()); } };
}
