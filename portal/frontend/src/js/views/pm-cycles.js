// PM cycles: the calendar of quarters, frozen snapshots, and (administrators) roll-over to the next quarter.
import { get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { date, int } from '../core/format.js';
import { toast } from '../core/editor.js';
import { isAdmin, hasLeadTools } from '../core/session.js';
import { openModal } from '../ui/modal.js';
import { errorBlock, pageHead, panel } from './common.js';
import { when } from './audit.js';

export function mountPmCycles(root) {
  let dead = false;
  const holder = h('div', { class: 'page' });
  const body = h('div', { class: 'grid' });
  root.append(holder);
  holder.append(pageHead('PM cycles and snapshots', 'A cycle is one financial-year quarter. Closing a quarter freezes a snapshot so quarters can be compared later.', [h('a', { class: 'btn', href: '#/pm' }, icon('arrow--left', 'sm'), 'Dashboard')]), body);

  async function load() {
    try {
      const d = await get('/api/pm/cycles');
      if (dead) return;
      draw(d);
    } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, load)); }
  }

  function draw(d) {
    const cur = d.current, r = d.rollover;
    const panels = [
      panel('This quarter', { cls: 'span-4' }, h('div', { class: 'kv' }, kv('Quarter', cur.label), kv('Starts', date(cur.start)), kv('Ends', date(cur.end)), kv('PM kick-off e-mail', date(cur.kickoff)))),
      panel('Cycles', { cls: 'span-8', flush: true }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Quarter', 'Starts', 'Ends', 'Kick-off', 'Kick-off e-mail', 'Status'].map((t) => h('th', null, t)))),
        h('tbody', null, d.cycles.map((c) => h('tr', null, h('td', null, c.quarter_label), h('td', null, date(c.start_date)), h('td', null, date(c.end_date)), h('td', null, date(c.kickoff_date)),
          h('td', null, c.kickoff_notified_at ? when(c.kickoff_notified_at) : h('span', { class: 'faint' }, 'not sent')), h('td', null, h('span', { class: 'badge ' + (c.status === 'OPEN' ? 'info' : 'mute') }, c.status === 'OPEN' ? 'Open' : 'Closed')))))))),
      panel('Snapshots', { cls: 'span-6', flush: true, hint: 'frozen pictures of the quarter' }, d.snapshots.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Quarter', 'As of', 'Assets'].map((t) => h('th', null, t)))),
        h('tbody', null, d.snapshots.map((s) => h('tr', null, h('td', null, s.quarter_label), h('td', null, date(s.as_of)), h('td', null, int(s.assets))))))) : h('div', { class: 'muted', style: { padding: '16px' } }, 'No snapshot yet. One is captured every Monday, after every asset import, and when a quarter closes.')),
    ];
    if (isAdmin() || hasLeadTools()) {
      panels.push(panel('Administration', { cls: 'span-6' }, h('div', { class: 'stack-v' },
        h('p', { class: 'muted' }, `${int(r.scope)} assets are in scope; ${int(r.with_pm)} have PM recorded for ${r.closing}, ${int(r.pending)} do not.`),
        h('div', { class: 'btn-row' },
          h('button', { class: 'btn', type: 'button', onClick: snapshot }, icon("report"), 'Capture snapshot now'),
          h('button', { class: 'btn danger', type: 'button', onClick: () => rollover(r) }, icon('renew'), `Close ${r.closing} and open ${r.opening}…`)),
        h('p', { class: 'hint' }, r.overdue ? `${r.closing} ended ${date(r.closing_end)} and has not been closed yet. A safety backup is taken first.`
          : `${r.opening} starts ${date(r.opening_start)} (${r.starts_in_days > 0 ? `in ${r.starts_in_days} days` : 'now'}). A safety backup is taken first.`))));
    }
    body.replaceChildren(...panels);
  }

  const kv = (k, v) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)];

  async function snapshot() {
    try { const r = await send('/api/pm/snapshot', {}); toast(`Snapshot saved: ${int(r.assets)} assets`); load(); } catch (e) { toast(e.message, 'bad'); }
  }

  function rollover(r) {
    const confirm = h('input', { id: 'ro-c', type: 'text', autocomplete: 'off', placeholder: 'ROLL OVER' });
    // the field displays upper-case regardless (CSS, like every text input on this page) - force the real value to match what's
    // shown, so what the admin sees typed is actually what gets sent, not silently different underneath
    confirm.addEventListener('blur', () => { confirm.value = confirm.value.trim().toUpperCase(); });
    const early = h('input', { type: 'checkbox', id: 'ro-e' });
    openModal({
      title: `Close ${r.closing} and open ${r.opening}`,
      lead: `Every in-scope asset returns to “PM pending” for the new quarter and its PM date, done-by and signed-by are cleared. Today's values are kept in the ${r.closing} snapshot and in the change log.`,
      body: h('div', null,
        h('div', { class: 'kv' }, kv('Assets affected', int(r.scope)), kv('PM recorded this quarter', int(r.with_pm)), kv('Still pending', int(r.pending))),
        r.starts_in_days > 0 ? h('label', { class: 'opt', for: 'ro-e' }, early, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, `The quarter has not ended (${r.starts_in_days} days left): cut over early`)) : null,
        h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'ro-c' }, 'Type ROLL OVER to confirm'), confirm)),
      actions: [{ label: 'Cancel' }, { label: 'Close quarter', danger: true, onClick: async () => {
        const out = await send('/api/pm/rollover', { confirm: confirm.value, early: early.checked });
        toast(`${out.closed} closed - ${out.opened} opened for ${int(out.assets)} assets`);
        load();
      } }],
    });
  }

  load();
  return { onLive: () => load(), destroy() { dead = true; } };
}

