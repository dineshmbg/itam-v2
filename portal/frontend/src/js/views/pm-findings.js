// PM findings: what the checklists turned up, the call each one needs, and when it was resolved.
import { get } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../ui/toast.js';
import { date, int, person } from '../core/format.js';
import * as router from '../core/router.js';
import { isAdmin } from '../core/session.js';
import { entity } from '../ui/hovercard.js';
import { errorBlock, kpiStrip, loading, pageHead } from './common.js';
import { SEVERITY, closeFindingDialog, linkCallDialog, setSeverity } from './pm-wo-common.js';

export function mountPmFindings(root) {
  let dead = false;
  const state = { status: router.current().params.get('status') || 'OPEN' };
  const kpis = h('div'), list = h('div', { class: 'tbl-wrap' });
  const status = h('select', { 'aria-label': 'Status', onChange: (e) => { state.status = e.target.value; router.setParams(state); load(); } },
    [['OPEN', 'Open'], ['CLOSED', 'Closed'], ['ALL', 'All']].map(([v, t]) => h('option', { value: v, selected: v === state.status }, t)));
  root.append(h('div', { class: 'page', 'data-mod': 'pm' }, pageHead('PM findings', 'Faults found while doing preventive maintenance. Major and critical ones need a call raised with CIPL; link its SR ID here.',
    [status, h('a', { class: 'btn', href: '#/pm/work' }, icon('arrow--left', 'sm'), 'Work orders')]), kpis, h('div', { class: 'spacer' }), h('section', { class: 'panel' }, list)));
  list.append(loading());

  async function load() {
    try {
      const d = await get('/api/pmwo/findings', { status: state.status });
      if (dead) return;
      const open = d.rows.filter((r) => r.status === 'OPEN');
      const needCall = open.filter((r) => r.call_required && !r.sr_id).length;
      kpis.replaceChildren(kpiStrip([
        { id: 'o', label: 'Open', value: int(open.length) },
        { id: 'c', label: 'Critical', value: int(open.filter((r) => r.severity === 'CRITICAL').length), tone: open.some((r) => r.severity === 'CRITICAL') ? 'bad' : 'ok' },
        { id: 'n', label: 'Need a call', value: int(needCall), tone: needCall ? 'warn' : 'ok', sub: 'no SR ID linked yet' }]));
      if (!d.rows.length) { list.replaceChildren(h('div', { class: 'muted', style: { padding: '16px' } }, 'No findings.')); return; }
      const again = () => load();
      list.replaceChildren(h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Severity', 'Asset', 'Finding', 'Engineer', 'Call', 'Status', ''].map((t) => h('th', null, t)))),
        h('tbody', null, d.rows.map((f) => {
          const [sw, st] = SEVERITY[f.severity];
          return h('tr', null, h('td', null, h('span', { class: 'badge ' + st }, sw)),
            h('td', null, entity('asset', f.asset_key, f.asset_key), h('div', null, h('a', { href: `#/pm/wo/${f.wo_id}`, class: 'mono' }, f.wo_no))),
            h('td', null, f.description, f.close_note ? h('div', { class: 'faint' }, `Resolved: ${f.close_note}`) : null, h('div', { class: 'faint' }, `${f.quarter_label} · ${date(f.created_at)}`)),
            h('td', null, f.engineer_name ? person(f.engineer_name) : '—'),
            h('td', null, f.sr_id ? h('span', { class: 'mono' }, f.sr_id) : f.call_required ? h('span', { class: 'badge warn' }, 'Call needed') : h('span', { class: 'faint' }, '—')),
            h('td', null, f.status === 'OPEN' ? h('span', { class: 'badge warn' }, 'Open') : h('span', { class: 'badge ok' }, 'Closed')),
            h('td', { class: 'right' }, f.status === 'OPEN' ? h('span', { class: 'btn-row tight' },
              !f.sr_id ? h('button', { class: 'btn', type: 'button', onClick: () => linkCallDialog({ finding: f, onDone: again }) }, icon('link'), 'Link call') : null,
              h('button', { class: 'btn', type: 'button', onClick: () => closeFindingDialog({ finding: f, onDone: again }) }, 'Close'),
              isAdmin() && f.severity !== 'CRITICAL' ? h('button', { class: 'btn ghost', type: 'button', onClick: () => setSeverity(f, f.severity === 'MINOR' ? 'MAJOR' : 'CRITICAL', again).catch((e) => toast(e.message, 'bad')) }, 'Raise severity') : null) : null));
        }))));
    } catch (e) { if (!dead) list.replaceChildren(errorBlock(e, load)); }
  }
  load();
  return { onLive: load, destroy() { dead = true; } };
}
