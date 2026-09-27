// A searchable, filterable, paged list backed by an endpoint that returns {rows, total, facets}. Used by the audit and activity logs.
import { get } from '../core/api.js';
import { h, icon, debounce } from '../core/dom.js';
import { int } from '../core/format.js';
import { createFacets } from '../ui/facets.js';
import { errorBlock, pageHead } from './common.js';

export function mountListPage(root, { title, sub, endpoint, facetDefs, columns, liveTables, empty = 'Nothing recorded yet.' }) {
  let dead = false, q = '', rows = [], total = 0, loading = false;
  let filters = Object.fromEntries(facetDefs.map((f) => [f.key, []]));
  const holder = h('div', { class: 'page' });
  root.append(holder);
  const search = h('input', { type: 'search', placeholder: 'Search this log', 'aria-label': 'Search this log', autocomplete: 'off', spellcheck: 'false' });
  const countEl = h('span', { class: 'count-line', 'aria-live': 'polite' });
  const body = h('tbody');
  const more = h('button', { class: 'btn', type: 'button', hidden: true, onClick: () => load(true) }, 'Show more');
  const facets = createFacets({
    facets: facetDefs.map((f) => ({ ...f, kind: 'single', limit: 12, labels: {} })),
    onChange: (f, clearAll) => { filters = clearAll ? Object.fromEntries(facetDefs.map((x) => [x.key, []])) : f; facets.setFilters(filters); load(false); },
  });
  const emptyEl = h('div', { class: 'empty', hidden: true }, icon('search'), h('h3', null, 'No matching entries'), h('div', null, empty));
  const table = h('div', { class: 'tbl-wrap grow' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, columns.map((c) => h('th', null, c.label)))), body), emptyEl);

  search.addEventListener('input', debounce(() => { q = search.value.trim(); load(false); }, 150));

  async function load(append) {
    if (loading) return;
    loading = true;
    const p = new URLSearchParams({ limit: '100', offset: String(append ? rows.length : 0) });
    if (q) p.set('q', q);
    facetDefs.forEach((f) => { if (filters[f.key].length) p.set(f.key, filters[f.key].join('|')); });
    if (!append) p.set('facets', '1');
    try {
      const res = await get(endpoint, p);
      if (dead) return;
      rows = append ? rows.concat(res.rows) : res.rows;
      total = res.total;
      if (res.facets) facets.update(Object.fromEntries(Object.entries(res.facets).map(([k, v]) => [k, v.map((x) => ({ v: String(x.v), n: x.n }))])), filters);
      draw();
    } catch (e) { if (!dead) holder.append(errorBlock(e)); } finally { loading = false; }
  }

  function draw() {
    countEl.replaceChildren(h('strong', null, int(total)), ` ${total === 1 ? 'entry' : 'entries'}`);
    body.replaceChildren(...rows.map((r) => h('tr', null, columns.map((c) => h('td', { class: c.cls || '' }, c.render(r))))));
    emptyEl.hidden = rows.length > 0;
    more.hidden = rows.length >= total;
  }

  holder.append(pageHead(title, sub),
    h('div', { class: 'register' }, facets.el,
      h('section', { class: 'results', 'aria-label': title }, h('div', { class: 'toolbar' }, h('div', { class: 'field' }, icon('search'), search), countEl), table, h('div', { class: 'more-row' }, more))));
  facets.setFilters(filters);
  load(false);
  return { onLive(ev) { if (ev.tables.some((t) => liveTables.includes(t))) load(false); }, destroy() { dead = true; } };
}
