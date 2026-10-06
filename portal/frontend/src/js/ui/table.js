// Virtualised data grid: only the visible rows exist in the DOM, pages of rows are fetched on demand, the header is sticky and sortable.
// Handles tens of thousands of rows at constant cost; rows are absolutely positioned on a fixed grid (rowHeight).
import { append, h, icon, hilite } from '../core/dom.js';
import { date, humanize, int, person } from '../core/format.js';
import { badge, classDotVar } from './badge.js';
import { entity } from './hovercard.js';

const WIDTH = { name: ['150px', '1fr'], mono: ['150px', '0.9fr'], date: ['112px', '0.5fr'], badge: ['172px', '0.8fr'], int: ['104px', '0.4fr'], text: ['168px', '1.4fr'] };

export function createTable({ columns, rowHeight = 30, pageSize = 100, fetchPage, onOpen, onSort, sort, search = () => '', ariaLabel = 'Results', rowClass }) {
  let total = 0;
  let epoch = 0;
  let sortState = { key: sort?.key, dir: sort?.dir || 'asc' };
  let active = -1;
  let selected = null;
  const pages = new Map();
  const loading = new Set();
  const rowEls = new Map();
  // Optional group heading rows ([{ start, n, label }] in data order). A heading takes one grid row of its own, so display index d and
  // data index i differ by the number of headings above; itemAt/dispOf convert, and every place that used a row index goes through them.
  let groups = [];

  const cols = columns.map((c) => WIDTH[c.kind] || WIDTH.text);
  const template = cols.map(([min, fr]) => `minmax(${min}, ${fr})`).join(' ');
  const tableW = cols.reduce((n, [min]) => n + parseInt(min, 10), 0);

  // Column fitting: the server sends the longest value of every free-text column; each is measured in the real font (upper case, as shown)
  // and the column gets exactly that width plus padding, never narrower than its own heading and never wider than MAX_FIT. Widths only grow
  // while the table lives, so filtering does not make the columns jump around.
  const MAX_FIT = 440;
  const fit = columns.map(() => 0);
  const measurer = document.createElement('canvas').getContext('2d');
  const textW = (txt, font, track) => { measurer.font = font; return measurer.measureText(String(txt).toUpperCase()).width + String(txt).length * track; };
  function headW(c) { return Math.ceil(textW(c.label, '600 11px "IBM Plex Sans", sans-serif', 0.66)) + 14 + 4 + 20 + 6; }
  function applyLongest(longest) {
    if (!longest) return;
    let changed = false;
    columns.forEach((c, i) => {
      if (c.kind !== 'text' && c.kind !== 'name' && c.kind !== 'mono') return;
      const v = longest[c.key] || (c.key === 'hostname' ? longest.asset_key : null);   // an empty hostname shows the asset number instead
      const font = c.kind === 'mono' ? '500 12.5px "IBM Plex Mono", monospace' : '500 12.5px "IBM Plex Sans", sans-serif';
      const w = Math.min(MAX_FIT, Math.max(headW(c), v ? Math.ceil(textW(v, font, c.kind === 'mono' ? 0 : 0.15)) + 20 + 10 : 0, 64));
      if (w > fit[i]) { fit[i] = w; changed = true; }
    });
    if (!changed) return;
    const px = columns.map((c, i) => (fit[i] ? fit[i] : parseInt(cols[i][0], 10)));
    inner.style.setProperty('--cols', px.map((w, i) => `minmax(${w}px, ${fit[i] ? (w / 100).toFixed(2) + 'fr' : cols[i][1]})`).join(' '));
    inner.style.setProperty('--table-w', px.reduce((a, b) => a + b, 0) + 'px');
  }

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
      case 'badge': td.append(badge(v, col.key)); break;
      default:
        td.title = v;
        if (col.key === 'asset_class') {
          const cv = classDotVar(v);
          append(td, [cv ? h('i', { class: 'cls-dot', style: { background: `var(${cv})` } }) : null, hilite(humanize(v), search())]);
        } else append(td, [hilite(v, search())]);
    }
    return td;
  }

  function itemAt(d) {
    let gi = -1;
    for (let k = 0; k < groups.length; k++) { if (groups[k].start + k <= d) gi = k; else break; }
    if (gi >= 0 && groups[gi].start + gi === d) return { head: groups[gi] };
    return { idx: d - (gi + 1) };
  }
  const dispOf = (i) => i + groups.filter((g) => g.start <= i).length;
  const dispTotal = () => total + groups.length;
  function setGroups(g) {
    groups = g || [];
    rowEls.forEach((el) => el.remove()); rowEls.clear();
    active = -1;
    rowsEl.style.height = dispTotal() * rowHeight + 'px';
  }

  function rowAt(i) { const p = pages.get(Math.floor(i / pageSize)); return p ? p[i % pageSize] : undefined; }

  function buildRow(d) {
    const it = itemAt(d);
    if (it.head) {
      return h('div', { class: 'gt-group', role: 'row', id: `gtr-${d}`, 'aria-rowindex': String(d + 2), dataset: { loaded: '1' }, style: { transform: `translateY(${d * rowHeight}px)` } },
        h('div', { class: 'gt-gcell', role: 'gridcell' }, h('strong', null, it.head.label), h('span', { class: 'n' }, int(it.head.n))));
    }
    const i = d, row = rowAt(it.idx);
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
      if (res.groups !== undefined) setGroups(res.groups);
      if (res.longest) (document.fonts?.ready || Promise.resolve()).then(() => applyLongest(res.longest));
      if (res.total != null && res.total !== total) setTotal(res.total);
      render(true);
    }).catch((e) => { loading.delete(page); showError(e); });
  }

  function setTotal(n) {
    total = n;
    rowsEl.style.height = dispTotal() * rowHeight + 'px';
    scroller.setAttribute('aria-rowcount', String(dispTotal() + 1));
    emptyEl.hidden = n > 0;
    scroller.hidden = n === 0;
    render();
  }

  function render(force = false) {
    const top = scroller.scrollTop;
    const view = scroller.clientHeight || 600;
    const first = Math.max(0, Math.floor(top / rowHeight) - 6);
    const last = Math.min(dispTotal() - 1, Math.ceil((top + view) / rowHeight) + 6);
    for (const [i, el] of rowEls) {
      if (i < first || i > last || (force && !el.dataset.loaded && rowAt(itemAt(i).idx))) { el.remove(); rowEls.delete(i); }
    }
    const need = new Set();
    for (let i = first; i <= last; i++) {
      if (!rowEls.has(i)) { const el = buildRow(i); rowsEl.append(el); rowEls.set(i, el); }
      const it = itemAt(i);
      if (it.idx != null) need.add(Math.floor(it.idx / pageSize));
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
    const row = rowAt(itemAt(i).idx);
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
      const D = dispTotal();
      let n = Math.min(D - 1, Math.max(0, (active < 0 ? (step > 0 ? -1 : 1) : active) + step));
      if (itemAt(n).head) n = Math.min(D - 1, Math.max(0, n + (step > 0 ? 1 : -1)));       // headings are not selectable
      if (itemAt(n).head) return;
      reveal(n); render(); setActive(n);
    } else if (e.key === 'Home') { e.preventDefault(); scroller.scrollTop = 0; render(); setActive(itemAt(0).head ? 1 : 0); }
    else if (e.key === 'End') { e.preventDefault(); scroller.scrollTop = dispTotal() * rowHeight; render(); setActive(dispTotal() - 1); }
    else if (e.key === 'Enter' && active >= 0) { const r = rowAt(itemAt(active).idx); if (r && onOpen) onOpen(r); }
  });

  function showError(e) {
    emptyEl.hidden = false;
    emptyEl.replaceChildren(icon('error--filled'), h('h3', null, 'Could not load data'), h('div', null, e.message || 'Unknown error'));
  }

  return {
    el: root,
    /** New query: forget everything and start again from the top. */
    reset(emptyText) {
      epoch++; pages.clear(); loading.clear(); groups = [];
      rowEls.forEach((el) => el.remove()); rowEls.clear();
      active = -1; scroller.scrollTop = 0;
      if (emptyText) emptyEl.replaceChildren(icon('search'), h('h3', null, emptyText.title), h('div', null, emptyText.hint || ''));
      setTotal(total || 1);
      ensure(0);
    },
    /** Live refresh: keep scroll position and current rows on screen, re-fetch only what is visible. */
    refresh() {
      epoch++;
      const visible = new Set([...rowEls.keys()].map((d) => itemAt(d).idx).filter((i) => i != null).map((i) => Math.floor(i / pageSize)));
      loading.clear();
      const my = epoch;
      visible.forEach((p) => {
        loading.add(p);
        fetchPage(p * pageSize, pageSize, p === 0).then((res) => {
          if (my !== epoch) return;
          loading.delete(p); pages.set(p, res.rows);
          if (res.groups !== undefined) setGroups(res.groups);
          if (res.longest) applyLongest(res.longest);
          if (res.total != null && res.total !== total) setTotal(res.total);
          rowEls.forEach((el, i) => { const it = itemAt(i); if (it.idx != null && Math.floor(it.idx / pageSize) === p) { el.remove(); rowEls.delete(i); } });
          render();
        }).catch(showError);
      });
      for (const p of [...pages.keys()]) if (!visible.has(p)) pages.delete(p);
    },
    setSelected(id) { selected = id; rowEls.forEach((el, i) => { if (id != null && rowAt(itemAt(i).idx)?.id === id) el.setAttribute('aria-selected', 'true'); else el.removeAttribute('aria-selected'); }); },
    setEmpty(t) { emptyEl.replaceChildren(icon('search'), h('h3', null, t.title), h('div', null, t.hint || '')); },
    setSort,
    get total() { return total; },
    focus() { scroller.focus(); },
  };
}
