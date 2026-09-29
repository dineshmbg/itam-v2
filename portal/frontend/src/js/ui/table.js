// Virtualised data grid: only the visible rows exist in the DOM, pages of rows are fetched on demand, the header is sticky and sortable.
// Handles tens of thousands of rows at constant cost; rows are absolutely positioned on a fixed grid (rowHeight).
import { append, h, icon, hilite } from '../core/dom.js';
import { date, humanize, int, person } from '../core/format.js';
import { badge, classDotVar } from './badge.js';
import { entity } from './hovercard.js';

const WIDTH = { name: ['150px', '1fr'], mono: ['150px', '0.9fr'], date: ['112px', '0.5fr'], badge: ['140px', '0.7fr'], int: ['104px', '0.4fr'], text: ['168px', '1.4fr'] };

export function createTable({ columns, rowHeight = 36, pageSize = 100, fetchPage, onOpen, onSort, sort, search = () => '', ariaLabel = 'Results', rowClass }) {
  let total = 0;
  let epoch = 0;
  let sortState = { key: sort?.key, dir: sort?.dir || 'asc' };
  let active = -1;
  let selected = null;
  const pages = new Map();
  const loading = new Set();
  const rowEls = new Map();

  const cols = columns.map((c) => WIDTH[c.kind] || WIDTH.text);
  const template = cols.map(([min, fr]) => `minmax(${min}, ${fr})`).join(' ');
  const tableW = cols.reduce((n, [min]) => n + parseInt(min, 10), 0);

  const headCells = columns.map((c) => {
    const btn = h('button', { class: 'gt-th' + (c.align === 'right' ? ' right' : ''), role: 'columnheader', type: 'button', 'data-key': c.key, 'aria-label': `Sort by ${c.label}` }, c.label, icon('chevron--sort', 'sm'));
    btn.addEventListener('click', () => {
      const dir = sortState.key === c.key && sortState.dir === 'asc' ? 'desc' : 'asc';
      setSort(c.key, dir);
      onSort?.(c.key, dir);
    });
    return btn;
  });
  const head = h('div', { class: 'gt-head', role: 'row' }, headCells);
  const rowsEl = h('div', { class: 'gt-rows', role: 'rowgroup' });
  const inner = h('div', { class: 'gt-inner', style: { '--cols': template, '--table-w': tableW + 'px', '--row-h': rowHeight + 'px' } }, head, rowsEl);
  const scroller = h('div', { class: 'gt-scroll', role: 'grid', tabindex: '0', 'aria-label': ariaLabel, 'aria-rowcount': '1' }, inner);
  const emptyEl = h('div', { class: 'empty', hidden: true });
  const root = h('div', { class: 'gt-root', style: { display: 'flex', flexDirection: 'column', minHeight: 0, flex: 1 } }, scroller, emptyEl);

  function setSort(key, dir) {
    sortState = { key, dir };
    headCells.forEach((b) => {
      const on = b.dataset.key === key;
      b.setAttribute('aria-sort', on ? (dir === 'asc' ? 'ascending' : 'descending') : 'none');
      b.querySelector('use').setAttribute('href', '#i-' + (on ? (dir === 'asc' ? 'chevron--sort--up' : 'chevron--sort--down') : 'chevron--sort'));
    });
  }
  if (sort?.key) setSort(sort.key, sortState.dir);

  function cell(col, row) {
    const v = row[col.key];
    const td = h('div', { class: 'gt-td' + (col.align === 'right' ? ' right' : ''), role: 'gridcell' });
    if (col.key === 'hostname' && (v == null || v === '')) {
      const key = row.asset_key ?? row.id;
      if (key) { td.title = 'No hostname recorded - showing the Asset (CI) number'; td.append(h('span', { class: 'mono faint' }, key)); return td; }
    }
    if (v == null || v === '') { td.append(h('span', { class: 'faint' }, '—')); return td; }
    switch (col.kind) {
      case 'name': td.title = v; append(td, [col.ref ? entity(col.ref, row[col.key + '__ref'] ?? v, hilite(person(v), search()), { mono: false }) : hilite(person(v), search())]); break;
      case 'mono': td.append(col.ref ? entity(col.ref, row[col.key + '__ref'] ?? v, hilite(v, search())) : h('span', { class: 'mono', title: v }, hilite(v, search()))); break;
      case 'date': td.append(date(v)); break;
      case 'int': td.append(int(v)); break;
      case 'money': td.append(Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })); break;
      case 'badge': td.append(badge(v)); break;
      default:
        td.title = v;
        if (col.key === 'asset_class') {
          const cv = classDotVar(v);
          append(td, [cv ? h('i', { class: 'cls-dot', style: { background: `var(${cv})` } }) : null, hilite(humanize(v), search())]);
        } else append(td, [hilite(v, search())]);
    }
    return td;
  }

  function rowAt(i) { const p = pages.get(Math.floor(i / pageSize)); return p ? p[i % pageSize] : undefined; }

  function buildRow(i) {
    const row = rowAt(i);
    const el = h('div', { class: 'gt-row' + (row ? '' : ' skel'), role: 'row', id: `gtr-${i}`, 'aria-rowindex': String(i + 2), style: { transform: `translateY(${i * rowHeight}px)` } });
    if (row) {
      columns.forEach((c) => el.append(cell(c, row)));
      if (row.id === selected) el.setAttribute('aria-selected', 'true');
      const rc = rowClass?.(row);
      if (rc) el.classList.add(rc);
      el.dataset.loaded = '1';
    } else columns.forEach(() => el.append(h('div', { class: 'gt-td', role: 'gridcell' })));
    if (i === active) el.classList.add('active');
    return el;
  }

  function ensure(page) {
    if (pages.has(page) || loading.has(page)) return;
    loading.add(page);
    const my = epoch;
    fetchPage(page * pageSize, pageSize, page === 0).then((res) => {
      if (my !== epoch) return;
      loading.delete(page);
      pages.set(page, res.rows);
      if (res.total != null && res.total !== total) setTotal(res.total);
      render(true);
    }).catch((e) => { loading.delete(page); showError(e); });
  }

  function setTotal(n) {
    total = n;
    rowsEl.style.height = n * rowHeight + 'px';
    scroller.setAttribute('aria-rowcount', String(n + 1));
    emptyEl.hidden = n > 0;
    scroller.hidden = n === 0;
    render();
  }

  function render(force = false) {
    const top = scroller.scrollTop;
    const view = scroller.clientHeight || 600;
    const first = Math.max(0, Math.floor(top / rowHeight) - 6);
    const last = Math.min(total - 1, Math.ceil((top + view) / rowHeight) + 6);
    for (const [i, el] of rowEls) {
      if (i < first || i > last || (force && !el.dataset.loaded && rowAt(i))) { el.remove(); rowEls.delete(i); }
    }
    const need = new Set();
    for (let i = first; i <= last; i++) {
      if (!rowEls.has(i)) { const el = buildRow(i); rowsEl.append(el); rowEls.set(i, el); }
      need.add(Math.floor(i / pageSize));
    }
    need.forEach(ensure);
  }

  scroller.addEventListener('scroll', () => render(), { passive: true });   // render() only touches rows that entered or left the window
  new ResizeObserver(() => render()).observe(scroller);

  rowsEl.addEventListener('click', (e) => {
    if (e.target.closest('a.ent')) return;                        // a reference link follows its link (its hover card shows the details)
    const el = e.target.closest('.gt-row');
    if (!el || !el.dataset.loaded) return;
    const i = +el.id.slice(4);
    setActive(i);
    const row = rowAt(i);
    if (row && onOpen) onOpen(row);
  });

  function setActive(i) {
    rowEls.get(active)?.classList.remove('active');
    active = i;
    rowEls.get(i)?.classList.add('active');
    scroller.setAttribute('aria-activedescendant', i >= 0 ? `gtr-${i}` : '');
  }
  function reveal(i) {
    const y = i * rowHeight;
    if (y < scroller.scrollTop + 36) scroller.scrollTop = Math.max(0, y - 36);
    else if (y + rowHeight > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = y + rowHeight - scroller.clientHeight;
  }
  scroller.addEventListener('keydown', (e) => {
    if (!total) return;
    const step = { ArrowDown: 1, ArrowUp: -1, PageDown: 12, PageUp: -12 }[e.key];
    if (step) {
      e.preventDefault();
      const n = Math.min(total - 1, Math.max(0, (active < 0 ? (step > 0 ? -1 : 1) : active) + step));
      reveal(n); render(); setActive(n);
    } else if (e.key === 'Home') { e.preventDefault(); scroller.scrollTop = 0; render(); setActive(0); }
    else if (e.key === 'End') { e.preventDefault(); scroller.scrollTop = total * rowHeight; render(); setActive(total - 1); }
    else if (e.key === 'Enter' && active >= 0) { const r = rowAt(active); if (r && onOpen) onOpen(r); }
  });

  function showError(e) {
    emptyEl.hidden = false;
    emptyEl.replaceChildren(icon('error--filled'), h('h3', null, 'Could not load data'), h('div', null, e.message || 'Unknown error'));
  }

  return {
    el: root,
    /** New query: forget everything and start again from the top. */
    reset(emptyText) {
      epoch++; pages.clear(); loading.clear();
      rowEls.forEach((el) => el.remove()); rowEls.clear();
      active = -1; scroller.scrollTop = 0;
      if (emptyText) emptyEl.replaceChildren(icon('search'), h('h3', null, emptyText.title), h('div', null, emptyText.hint || ''));
      setTotal(total || 1);
      ensure(0);
    },
    /** Live refresh: keep scroll position and current rows on screen, re-fetch only what is visible. */
    refresh() {
      epoch++;
      const visible = new Set([...rowEls.keys()].map((i) => Math.floor(i / pageSize)));
      loading.clear();
      const my = epoch;
      visible.forEach((p) => {
        loading.add(p);
        fetchPage(p * pageSize, pageSize, p === 0).then((res) => {
          if (my !== epoch) return;
          loading.delete(p); pages.set(p, res.rows);
          if (res.total != null && res.total !== total) setTotal(res.total);
          rowEls.forEach((el, i) => { if (Math.floor(i / pageSize) === p) { el.remove(); rowEls.delete(i); } });
          render();
        }).catch(showError);
      });
      for (const p of [...pages.keys()]) if (!visible.has(p)) pages.delete(p);
    },
    setSelected(id) { selected = id; rowEls.forEach((el, i) => { if (id != null && rowAt(i)?.id === id) el.setAttribute('aria-selected', 'true'); else el.removeAttribute('aria-selected'); }); },
    setEmpty(t) { emptyEl.replaceChildren(icon('search'), h('h3', null, t.title), h('div', null, t.hint || '')); },
    setSort,
    get total() { return total; },
    focus() { scroller.focus(); },
  };
}
