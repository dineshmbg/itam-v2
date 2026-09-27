// System-wide search: results appear while typing (no button), grouped by register, fully keyboard driven.
import { get } from '../core/api.js';
import { h, icon, debounce, hilite } from '../core/dom.js';
import { int } from '../core/format.js';
import { go } from '../core/router.js';

function target(group, item) {
  if (group.dataset === 'engineers') return ['dashboard/engineers', { eng: item.id }];
  return ['registers/' + group.dataset, { open: item.id }];
}

export function createSearch() {
  const input = h('input', { type: 'search', id: 'gsearch', placeholder: 'Search assets, calls, parts, RMA, engineers', autocomplete: 'off', spellcheck: 'false', role: 'combobox', 'aria-expanded': 'false', 'aria-controls': 'gs-pop', 'aria-autocomplete': 'list', 'aria-label': 'Search everything' });
  const clearBtn = h('button', { class: 'clear', type: 'button', 'aria-label': 'Clear search', hidden: true }, icon('close'));
  const pop = h('div', { class: 'gs-pop', id: 'gs-pop', role: 'listbox', hidden: true });
  const el = h('div', { class: 'gsearch' }, h('div', { class: 'field' }, icon('search'), input, clearBtn, h('span', { class: 'kbd', 'aria-hidden': 'true' }, '/')), pop);
  let items = [];
  let idx = -1;
  let ctl;
  let lastQ = '';

  const close = () => { pop.hidden = true; input.setAttribute('aria-expanded', 'false'); idx = -1; };
  const open = () => { pop.hidden = false; input.setAttribute('aria-expanded', 'true'); };

  function mark(i) {
    items.forEach((it, n) => it.el.setAttribute('aria-selected', String(n === i)));
    idx = i;
    if (i >= 0) { items[i].el.scrollIntoView({ block: 'nearest' }); input.setAttribute('aria-activedescendant', items[i].el.id); }
  }

  function choose(it) { const [path, params] = target(it.group, it.item); close(); input.blur(); go(path, params); }

  function render(res, q) {
    items = [];
    pop.replaceChildren();
    if (!res.groups.length) { pop.append(h('div', { class: 'gs-empty' }, `No results for “${q}”.`)); open(); return; }
    let n = 0;
    for (const g of res.groups) {
      const box = h('div', { class: 'gs-group', role: 'group', 'aria-label': g.label });
      const more = g.total > g.items.length && g.dataset !== 'engineers'
        ? h('a', { href: `#/registers/${g.dataset}?q=${encodeURIComponent(q)}`, onClick: () => { close(); } }, `All ${int(g.total)} →`) : h('span', null, int(g.total));
      box.append(h('h3', null, h('span', null, g.label), more));
      for (const item of g.items) {
        const row = h('div', { class: 'gs-item', role: 'option', id: `gs-${n}`, 'aria-selected': 'false' }, h('span', { class: 't' }, hilite(item.title, q)), h('span', { class: 's' }, hilite(item.sub || '', q)));
        const it = { el: row, group: g, item };
        row.addEventListener('mousedown', (e) => { e.preventDefault(); choose(it); });
        row.addEventListener('mousemove', () => mark(items.indexOf(it)));
        items.push(it);
        box.append(row);
        n++;
      }
      pop.append(box);
    }
    open();
    mark(0);
  }

  const run = debounce(async () => {
    const q = input.value.trim();
    clearBtn.hidden = !q;
    if (q.length < 2) { close(); return; }
    if (q === lastQ && !pop.hidden) return;
    lastQ = q;
    ctl?.abort();
    ctl = new AbortController();
    try { render(await get('/api/search', { q }, { signal: ctl.signal }), q); } catch (e) { if (e.name !== 'AbortError') { pop.replaceChildren(h('div', { class: 'gs-empty' }, 'Search is unavailable.')); open(); } }
  }, 110);

  input.addEventListener('input', run);
  input.addEventListener('focus', () => { if (items.length && input.value.trim().length >= 2) open(); });
  input.addEventListener('blur', () => setTimeout(close, 120));
  clearBtn.addEventListener('mousedown', (e) => { e.preventDefault(); input.value = ''; clearBtn.hidden = true; close(); input.focus(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (pop.hidden && items.length) open(); mark(Math.min(items.length - 1, idx + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); mark(Math.max(0, idx - 1)); }
    else if (e.key === 'Enter') { if (idx >= 0 && items[idx] && !pop.hidden) { e.preventDefault(); choose(items[idx]); } }
    else if (e.key === 'Escape') { if (!pop.hidden) { close(); } else { input.value = ''; clearBtn.hidden = true; input.blur(); } }
  });

  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
    if ((e.key === '/' && !typing) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) { e.preventDefault(); input.focus(); input.select(); }
  });

  return { el, focus: () => input.focus() };
}
