// Sign-in message for an engineer when a new inventory analysis has been shared: their own figures and most urgent machines, with a way into the report.
import { get, send } from '../core/api.js';
import { h } from '../core/dom.js';
import { go } from '../core/router.js';
import { int, pct, when } from '../core/format.js';
import { openModal } from './modal.js';

export async function showMatchNotice() {
  let n;
  try { n = (await get('/api/match/notice')).notice; } catch (_) { return; }
  const key = 'itam.matchNotice.' + (n?.run_id || '');
  try { if (!n || sessionStorage.getItem(key)) return; sessionStorage.setItem(key, '1'); } catch (_) { if (!n) return; }   // once per browser session; the server remembers once it has been opened
  const s = n.summary;
  const seen = () => send('/api/match/notice/seen', { run_id: n.run_id }).catch(() => {});
  const stat = (k, v, tone) => h('div', { class: 'k-card' }, h('div', { class: 'muted' }, k), h('strong', { class: tone || null }, v));
  const urgent = n.urgent.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
    h('thead', null, h('tr', null, ['Asset (CI)', 'User', 'Location', 'What to do'].map((c) => h('th', null, c)))),
    h('tbody', null, n.urgent.map((r) => h('tr', null, h('td', null, r.ci), h('td', null, r.user || ''), h('td', null, r.location || ''), h('td', { class: 'wrap' }, r.action || ''))))))
    : null;
  openModal({ title: `${s.tool} report shared with you`, wide: true,
    lead: `Report of ${s.report_as_of || ''}, shared ${when(n.shared_at)}. These figures are for the machines assigned to you only.`,
    body: h('div', null,
      h('div', { class: 'btn-row' }, stat('Your machines', int(s.machines)), stat('In the report', `${int(s.installed)} (${pct(s.coverage)})`), stat('To install', int(s.missing), s.missing ? 'bad' : 'ok'),
        stat('Needs attention', int(s.attention), s.attention ? 'warn' : 'ok'), stat('Deployed, no agent', int(s.high), s.high ? 'bad' : 'ok')),
      h('ul', null, s.findings.map((f) => h('li', null, h('strong', null, f.severity + ': '), f.finding, ' ', f.action))),
      urgent ? h('p', { class: 'muted' }, `Most urgent (${int(n.urgent.length)} of ${int(s.high)} shown):`) : null, urgent),
    actions: [{ label: n.needs ? 'Remind me at next sign-in' : 'Close', onClick: () => { if (!n.needs) seen(); } },
      { label: 'Open my inventory report', primary: true, onClick: () => { seen(); go('reports/inventory'); } }] });
}
