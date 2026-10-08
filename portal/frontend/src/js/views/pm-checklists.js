// PM checklists: what is checked for each class of asset, and the rules that go with it. Read-only for now.
import { get } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { errorBlock, loading, pageHead, panel } from './common.js';

const EVERY = { 1: 'Every quarter', 2: 'Every 2nd quarter (Apr-Jun, Oct-Dec)', 4: 'Once a year (Apr-Jun)' };

export function mountPmChecklists(root) {
  let dead = false;
  const body = h('div', { class: 'grid' });
  root.append(h('div', { class: 'page', 'data-mod': 'pm' }, pageHead('PM checklists', 'The tasks asked for each class of asset. A work order freezes the list when work starts, so later changes never alter a PM already done.',
    [h('a', { class: 'btn', href: '#/pm/work' }, icon('arrow--left', 'sm'), 'Work orders')]), body));
  body.append(loading());
  get('/api/pmwo/checklists').then((d) => {
    if (dead) return;
    const rules = panel('Rules', { cls: 'span-12' }, h('ul', { class: 'wo-rules' },
      h('li', null, h('strong', null, 'Criticality: '), Object.entries(d.criticality).map(([k, v]) => `${k} = ${v}`).join(', '), '. Every other class is C.'),
      h('li', null, h('strong', null, 'Technical verification by a second person: '), Object.entries(d.sample_pct).map(([k, v]) => `criticality ${k}: ${v}%`).join(', '), ' (a fixed, repeatable sample - the same asset is always in or out for a quarter).'),
      h('li', null, h('strong', null, 'Owner acknowledgement: '), `deemed accepted after ${d.ack_days} days without an answer.`)));
    body.replaceChildren(rules, ...d.checklists.map((c) => panel(`${c.name} (${c.asset_class}, v${c.version})`, { cls: 'span-6', flush: true, hint: c.status === 'ACTIVE' ? 'active' : 'retired' }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['#', 'Task', 'Rules', 'How often'].map((t) => h('th', null, t)))),
      h('tbody', null, c.tasks.map((t) => h('tr', null, h('td', null, t.seq), h('td', null, t.text), h('td', null,
        t.mandatory ? h('span', { class: 'badge info' }, 'Mandatory') : h('span', { class: 'faint' }, 'Optional'), t.critical ? h('span', { class: 'badge bad', style: { marginLeft: '4px' } }, 'Critical') : null,
        t.kind === 'VALUE' ? h('div', { class: 'faint' }, `Reading in ${t.unit || 'units'}${t.min_value != null || t.max_value != null ? `, ${t.min_value ?? '…'} to ${t.max_value ?? '…'}` : ''}`) : null), h('td', null, EVERY[t.every_n])))))))));
  }).catch((e) => { if (!dead) body.replaceChildren(errorBlock(e)); });
  return { destroy() { dead = true; } };
}
