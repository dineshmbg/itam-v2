import { get } from '../core/api.js';
import { h, icon, clear } from '../core/dom.js';
import { bytes, date, int, clock } from '../core/format.js';
import { tickCross } from '../ui/badge.js';
import { errorBlock, pageHead, panel, loading, regHref } from './common.js';

const STATUS = { pass: ['checkmark--filled', 'tick', 'Pass'], warn: ['warning--filled', 'caution', 'Needs review'], fail: ['error--filled', 'cross', 'Fails'] };

export function mountIntegrity(root) {
  let dead = false;
  const sub = h('span', null, '');
  const body = h('div', null);
  root.append(h('div', { class: 'page' }, pageHead('Data integrity', sub), body));
  body.append(loading());

  function draw(d) {
    const s = d.summary;
    clear(body);
    const summary = h('div', { class: 'panel', style: { padding: '16px' } }, h('div', { class: 'sum-strip' },
      ...['pass', 'warn', 'fail'].map((k) => h('div', { class: 'it' }, h('span', { class: STATUS[k][1] }, icon(STATUS[k][0], 'lg')), h('strong', null, int(s[k])), h('span', { class: 'muted' }, { pass: 'checks pass', warn: 'need review', fail: 'fail' }[k]))),
      h('div', { class: 'it' }, tickCross(s.files_ok === s.files_total, 'All reconcile', 'Mismatch'), h('strong', null, `${s.files_ok}/${s.files_total}`), h('span', { class: 'muted' }, 'masters/ sheets reconcile with the database'))));

    const checks = d.checks.map((c) => {
      const [ic, cls, txt] = STATUS[c.status];
      return h('div', { class: 'integrity-row' }, h('span', { class: cls, title: txt }, icon(ic, 'lg'), h('span', { class: 'sr-only' }, txt)),
        h('div', null, h('div', { class: 'ttl' }, c.title), h('div', { class: 'det' }, c.detail), c.status !== 'pass' && c.note ? h('div', { class: 'note' }, c.note) : null,
          c.sample?.length ? h('div', { class: 'note' }, 'e.g. ', ...c.sample.flatMap((k, i) => [i ? ', ' : '', h('a', { class: 'mono', href: regHref('assets', { open: k }) }, k)])) : null),
        h('div', { class: 'res' }, c.bad ? h('strong', null, int(c.bad)) : '0', h('span', { class: 'faint' }, ` of ${int(c.total)}`)));
    });

    const freshRows = d.freshness.map((f) => h('tr', null, h('td', null, f.name),
      h('td', null, date(f.as_of), f.age_days != null ? h('span', { class: 'faint' }, ` · ${f.age_days} d ago`) : null), h('td', { class: 'right' }, int(f.n))));
    const fresh = h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, h('th', null, 'Dataset'), h('th', null, 'Snapshot'), h('th', { class: 'right' }, 'Rows'))), h('tbody', null, freshRows)));

    const fileRows = d.masters.map((m) => h('tr', null, h('td', null, tickCross(m.status === 'pass', 'Reconciles', 'Row counts differ')),
      h('td', { class: 'wrap' }, h('div', null, m.label), h('div', { class: 'faint mono', style: { fontSize: '11.5px' } }, `${m.file} · ${m.sheet} · ${bytes(m.size)}`)),
      h('td', { class: 'right' }, int(m.in_file)), h('td', { class: 'right' }, int(m.in_db))));
    const files = h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, h('th', null, ''), h('th', null, 'File / sheet'), h('th', { class: 'right' }, 'In file'), h('th', { class: 'right' }, 'In DB'))), h('tbody', null, fileRows)));

    body.append(summary, h('div', { class: 'grid' },
      panel('Referential and consistency checks', { cls: 'span-7', flush: true, hint: 'evaluated live on the database' }, ...checks),
      h('div', { class: 'span-5', style: { display: 'grid', gap: '16px', alignContent: 'start' } },
        panel('Data freshness', { flush: true }, fresh), panel('Masters files vs database', { flush: true, hint: 'row counts must match' }, files))));
    sub.textContent = `Checked ${clock(Date.now())} · re-evaluated automatically whenever the data changes`;
  }

  async function load() {
    try { const d = await get('/api/integrity'); if (!dead) draw(d); } catch (e) { if (!dead) { clear(body); body.append(errorBlock(e, load)); } }
  }
  load();
  return { onLive: load, destroy() { dead = true; } };
}
