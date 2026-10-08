// My inventory report: an engineer's own part of an inventory match (BigFix, Trend Micro...) - only the machines assigned to them.
// An administrator can preview any engineer's view. Analyses appear here once an administrator has shared them with engineers.
import { download, get } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../ui/toast.js';
import { int, pct, titleCase, when } from '../core/format.js';
import { errorBlock, kpiStrip, loading, pageHead, panel } from './common.js';

const SEV = { Critical: 'bad', 'Needs attention': 'warn', Good: 'ok', Information: 'info' };
const PRIORITY = { High: 'bad', Medium: 'warn', Low: 'mute' };

export function mountInventoryMine(root) {
  let dead = false, runs = [], runId = null, eng = null, engineers = [], admin = false;
  const body = h('div', null);
  const picks = h('div', { class: 'btn-row' });
  const sub = h('span', null, '');
  root.append(h('div', { class: 'page' }, pageHead('My inventory report', sub, picks), body));
  body.append(loading());

  const table = (cols, rows, limit = 200) => (rows.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, cols.map((c) => h('th', { class: c[2] || null }, c[1])))),
    h('tbody', null, rows.slice(0, limit).map((r) => h('tr', null, cols.map(([k, , cls, fn]) => h('td', { class: cls || null }, fn ? fn(r) : (r[k] ?? '')))))))) : h('div', { class: 'muted', style: { padding: '16px' } }, 'Nothing here - good.'));
  const kv = (k, v) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)];

  async function load() {
    try {
      const d = await get('/api/match/mine', { as: eng || undefined, run: runId || undefined });
      if (dead) return;
      runs = d.runs; admin = d.admin; engineers = d.engineers || engineers; eng = d.engineer || eng;
      if (!runs.length) { body.replaceChildren(h('div', { class: 'panel' }, h('div', { class: 'panel-b' }, h('p', { class: 'muted' }, admin ? 'No analysis yet. Run one under Data tools > Inventory match.' : 'Nothing has been shared with you yet. When the centre\'s report has been analysed and shared, your machines appear here.')))); sub.textContent = ''; return; }
      if (!runId || !runs.some((r) => r.run_id === runId)) runId = runs[0].run_id;
      drawPickers();
      await show();
    } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, load)); }
  }

  function drawPickers() {
    const rs = h('select', { id: 'mi-run', 'aria-label': 'Analysis' }, runs.map((r) => h('option', { value: r.run_id, selected: r.run_id === runId }, `${r.tool} · ${when(r.published_at || r.at)}${admin && !r.published_at ? ' (not shared)' : ''}`)));
    rs.onchange = () => { runId = rs.value; show(); };
    const items = [rs];
    if (admin) {
      const es = h('select', { id: 'mi-eng', 'aria-label': 'Engineer' }, h('option', { value: '' }, 'Choose an engineer…'), engineers.map((e) => h('option', { value: e.key, selected: e.key === eng }, `${titleCase(e.name)} (${int(e.machines)})`)));
      es.onchange = () => { eng = es.value || null; show(); };
      items.push(es);
    }
    picks.replaceChildren(...items, h('button', { class: 'btn primary', type: 'button', onClick: dlx }, icon('document--export'), 'Download my Excel'));
  }

  async function dlx() { try { await download('/api/match/mine/export', { run_id: runId, as: eng || undefined }, 'my-inventory.xlsx'); } catch (e) { toast(e.message, 'bad'); } }

  async function show() {
    if (admin && !eng) { body.replaceChildren(h('div', { class: 'panel' }, h('div', { class: 'panel-b' }, h('p', { class: 'muted' }, 'Choose an engineer to see what they see.')))); return; }
    body.replaceChildren(loading());
    try {
      const d = await get('/api/match/mine/' + runId, { as: admin ? eng : undefined });
      if (dead) return;
      const s = d.summary;
      sub.textContent = `${s.tool} · report of ${s.report_as_of || ''} · ${d.filename}`;
      const miss = d.assets.filter((r) => !r.installed).sort((a, b) => ['High', 'Medium', 'Low'].indexOf(a.priority) - ['High', 'Medium', 'Low'].indexOf(b.priority));
      const attn = d.assets.filter((r) => r.health === 'Needs attention').sort((a, b) => (b.r_age || 0) - (a.r_age || 0));
      const named = miss.filter((r) => r.possible);
      const tone = s.high ? 'bad' : (s.missing || s.attention) ? 'warn' : 'ok';
      const prio = (r) => (r.priority ? h('span', { class: 'badge ' + PRIORITY[r.priority] }, r.priority) : '');
      const tabs = [
        ['Install the agent', miss.length, () => table([['ci', 'Asset (CI)'], ['class', 'Class', null, (r) => titleCase(r.class)], ['model', 'Model'], ['user', 'User', null, (r) => titleCase(r.user)], ['location', 'Location'], ['status', 'Status', null, (r) => titleCase(r.status.replace(/_/g, ' '))], ['priority', 'Priority', null, prio], ['action', 'What to do', 'wrap wide']], miss)],
        ['Installed, needs attention', attn.length, () => table([['ci', 'Asset (CI)'], ['user', 'User', null, (r) => titleCase(r.user)], ['location', 'Location'], ['r_seen', 'Last report'], ['issues', 'Problem', 'wrap wide'], ['action', 'What to do', 'wrap wide']], attn)],
        ['Probably the same machine', named.length, () => table([['ci', 'Asset (CI) in register'], ['possible', 'Name in report'], ['possible_by', 'Because', 'wrap'], ['action', 'What to do', 'wrap wide']], named)],
        ['All my machines', d.assets.length, () => table([['ci', 'Asset (CI)'], ['class', 'Class', null, (r) => titleCase(r.class)], ['user', 'User', null, (r) => titleCase(r.user)], ['location', 'Location'], ['health', 'In the report', null, (r) => (r.health === 'Healthy' ? 'Yes, healthy' : r.health === 'Needs attention' ? 'Yes, needs attention' : 'No')], ['r_seen', 'Last report']], d.assets, 500)],
      ];
      const pane = h('div', null);
      const bar = h('div', { class: 'mtabs', role: 'tablist' });
      const pick = (i) => { [...bar.children].forEach((b, j) => b.setAttribute('aria-selected', String(i === j))); pane.replaceChildren(tabs[i][2]()); };
      tabs.forEach(([name, n], i) => bar.append(h('button', { class: 'mtab', type: 'button', role: 'tab', onClick: () => pick(i) }, name, h('span', { class: 'count' }, int(n)))));
      body.replaceChildren(
        kpiStrip([{ id: 'm', label: 'My machines checked', value: int(s.machines) }, { id: 'i', label: 'In the report', value: int(s.installed), tone: 'ok', sub: `${int(s.healthy)} healthy` },
          { id: 'x', label: 'To install', value: int(s.missing), tone: s.missing ? 'bad' : 'ok', sub: `${int(s.high)} deployed to users` }, { id: 'a', label: 'Needs attention', value: int(s.attention), tone: s.attention ? 'warn' : 'ok' },
          { id: 'c', label: 'Coverage', value: pct(s.coverage), tone, sub: `target ${pct(s.target)}` }]),
        h('div', { class: 'grid' },
          panel('What to do', { cls: 'span-12', flush: true }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, h('th', null, 'Rating'), h('th', null, 'Finding'), h('th', null, 'Action'))),
            h('tbody', null, s.findings.map((f) => h('tr', null, h('td', null, h('span', { class: 'badge ' + (SEV[f.severity] || 'mute') }, f.severity)), h('td', { class: 'wrap' }, f.finding), h('td', { class: 'wrap' }, f.action))))))),
          panel('My machines', { cls: 'span-12', flush: true, hint: 'only machines assigned to this engineer' }, bar, pane)));
      pick(0);
    } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, show)); }
  }

  load();
  return { destroy() { dead = true; }, onLive: () => {} };
}
