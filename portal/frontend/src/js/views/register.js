// Generic register: live facets + instant search + virtual table + detail drawer for any dataset described by /api/meta.
import { downloadGet, get } from '../core/api.js';
import { h, icon, debounce } from '../core/dom.js';
import { int } from '../core/format.js';
import { setParams, patchParam, current } from '../core/router.js';
import { openDrawer } from '../ui/drawer.js';
import { createFacets, optionLabel, BLANK } from '../ui/facets.js';
import { clearSavedWidths, createTable } from '../ui/table.js';
import { labelOf } from '../ui/badge.js';
import { errorBlock, pageHead } from './common.js';
import { getSchema } from '../core/editor.js';
import { mountRecord, newRecord } from './record.js';
import { recordPmDialog } from './pm-record.js';
import { assignEngineerDialog } from './assign-engineer.js';
import { toast } from '../core/editor.js';
import { columnsDialog, loadColumns } from '../ui/columns.js';
import { user as sessionUser, isAdmin } from '../core/session.js';

const TABLE_OF = { assets: 'asset', pm: 'asset', calls: 'svc_call', inward: 'spare_inward', outward: 'spare_outward', rma: 'oem_rma', employees: 'employee', engineers: 'portal_engineer' };
let metaP;
export const getMeta = () => (metaP ||= get('/api/meta'));

export function mountRegister(root, name, opts = {}) {
  let dead = false, table, facets, drawer, meta, ds, rec = null, openId = null;
  let q = '', sort = null, filters = {}, archived = false;
  const holder = h('div', { class: 'page' });
  root.append(holder);

  function readUrl() {
    const p = current().params;
    q = p.get('q') || '';
    archived = p.get('scope') === 'archived';
    sort = p.get('sort') ? { key: p.get('sort'), dir: p.get('dir') || 'asc' } : { key: ds.sort[0], dir: ds.sort[1] };
    filters = {};
    ds.facets.forEach((f) => {
      const raw = p.get('f.' + f.key);
      if (raw !== null) filters[f.key] = raw.split('|').filter(Boolean);
      else if (ds.defaults[f.key]) filters[f.key] = [...ds.defaults[f.key]];
      else filters[f.key] = [];
    });
  }

  function paramsForServer(offset, limit, first) {
    const p = new URLSearchParams();
    if (q) p.set('q', q);
    ds.facets.forEach((f) => {
      const v = filters[f.key] || [];
      if (v.length) p.set('f.' + f.key, v.join('|'));
      else if (ds.defaults[f.key]) p.set('f.' + f.key, '');
    });
    p.set('sort', sort.key); p.set('dir', sort.dir);
    p.set('limit', String(limit)); p.set('offset', String(offset));
    if (archived) p.set('scope', 'archived');
    if (first) p.set('facets', '1');
    return p;
  }

  function syncUrl() {
    const o = {};
    if (q) o.q = q;
    if (archived) o.scope = 'archived';
    if (sort.key !== ds.sort[0] || sort.dir !== ds.sort[1]) { o.sort = sort.key; o.dir = sort.dir; }
    ds.facets.forEach((f) => {
      const v = filters[f.key] || [];
      const def = ds.defaults[f.key] || [];
      if (v.join('|') !== def.join('|')) o['f.' + f.key] = v.join('|');
    });
    const open = current().params.get('open');
    if (open) o.open = open;
    setParams(o);
  }

  const countEl = h('span', { class: 'count-line', 'aria-live': 'polite' }, '');
  const search = h('input', { type: 'search', placeholder: 'Search this register', 'aria-label': `Search ${name}`, autocomplete: 'off', spellcheck: 'false' });
  const clearBtn = h('button', { class: 'clear', type: 'button', 'aria-label': 'Clear search', hidden: true }, icon('close'));
  const chipsEl = h('div', { class: 'chips', hidden: true, 'aria-label': 'Active filters' });
  const runSearch = debounce(() => { q = search.value.trim(); clearBtn.hidden = !search.value; syncUrl(); requery(); }, 120);
  search.addEventListener('input', runSearch);
  clearBtn.addEventListener('click', () => { search.value = ''; q = ''; clearBtn.hidden = true; syncUrl(); requery(); search.focus(); });

  function drawChips() {
    const chips = [];
    ds.facets.forEach((f) => {
      const def = ds.defaults[f.key] || [];
      (filters[f.key] || []).forEach((v) => {
        if (def.includes(v)) return;
        chips.push(h('span', { class: 'chip' }, h('b', null, f.label + ':'), optionLabel(f.key, v, f.labels),
          h('button', { type: 'button', 'aria-label': `Remove filter ${f.label} ${optionLabel(f.key, v, f.labels)}`, onClick: () => { filters = { ...filters, [f.key]: filters[f.key].filter((x) => x !== v) }; applyFilters(); } }, icon('close', 'sm'))));
      });
      if (def.length && !(filters[f.key] || []).length) chips.push(h('span', { class: 'chip' }, h('b', null, f.label + ':'), 'All', h('button', { type: 'button', 'aria-label': `Restore default for ${f.label}`, onClick: () => { filters = { ...filters, [f.key]: [...def] }; applyFilters(); } }, icon('restart', 'sm'))));
    });
    chipsEl.hidden = chips.length === 0;
    chipsEl.replaceChildren(...chips);
  }

  function applyFilters() { syncUrl(); facets.setFilters(filters); drawChips(); requery(); }

  function requery() {
    table.reset(q || Object.values(filters).some((v) => v.length) ? { title: 'No matching records', hint: 'Try a different search or remove a filter.' } : { title: 'No records', hint: '' });
  }

  // Group heading rows (e.g. Blank / Raised / Resolved on the call register) - only while the list is sorted by the grouping column,
  // sized from the status facet's counts so every heading shows the whole group's count, not just the page loaded. The counts must add
  // up to the list's total, otherwise no headings are drawn rather than wrong ones.
  function groupHeadings(res) {
    const g = ds.group;
    if (!g || sort.key !== (g.col || g.key) || !res.facets?.[g.key]) return [];
    const n = Object.fromEntries(res.facets[g.key].map((i) => [i.v, i.n]));
    const sel = filters[g.key] || [];
    const order = (sort.dir === 'desc' ? [...g.order].reverse() : g.order).filter((v) => n[v] > 0 && (!sel.length || sel.includes(v)));
    let start = 0;
    const out = order.map((v) => { const o = { start, n: n[v], label: v === BLANK ? 'Blank' : labelOf(v, g.key) }; start += n[v]; return o; });
    return start === res.total ? out : [];
  }

  async function fetchPage(offset, limit, first) {
    const res = await get('/api/registers/' + name, paramsForServer(offset, limit, first));
    if (dead) return { rows: [], total: 0 };
    if (first) {
      countEl.replaceChildren(h('strong', null, int(res.total)), ` ${res.total === 1 ? 'record' : 'records'}`);
      if (res.facets) facets.update(res.facets, filters);
      res.groups = groupHeadings(res);
    }
    return res;
  }

  async function openDetail(id) {
    drawer?.close(true);
    rec = null; openId = id;
    patchParam('open', id);
    table?.setSelected(id);
    drawer = openDrawer({ wide: true, title: id, subtitle: 'Loading…', onClose: () => { openId = null; rec = null; patchParam('open', null); table?.setSelected(null); } });
    try {
      const dn = ds.detail || name;                       // a worklist opens the record of the register it is drawn from
      const [d, schema] = await Promise.all([get(`/api/registers/${dn}/${encodeURIComponent(id)}`), getSchema()]);
      if (dead || openId !== id) return;
      drawer.setTitle(id, meta[name].label.replace(/s$/, ''));
      rec = await mountRecord({ drawer, name: dn, payload: d, schema, onChange: () => table?.refresh(), onOpen: (key) => openDetail(key) });
    } catch (e) { drawer.body.replaceChildren(errorBlock(e)); }
  }

  (async () => {
    try {
      meta = await getMeta();
      if (dead) return;
      ds = meta[name];
      readUrl();
      const schema = await getSchema().catch(() => null);
      const tools = [];
      const arch = h('input', { type: 'checkbox', 'aria-label': 'Show archived records' });
      arch.checked = archived;
      arch.addEventListener('change', () => { archived = arch.checked; syncUrl(); requery(); });
      tools.push(h('label', { class: 'pill-toggle' }, arch, icon('checkbox', 'off'), icon('checkbox--checked--filled', 'on'), 'Show archived'));
      if (name === 'pm') tools.push(h('button', { class: 'btn primary', type: 'button', onClick: async () => {
        const keys = await api.allKeys(500);
        if (!keys.length) { toast('No assets are listed.', 'bad'); return; }
        recordPmDialog({ keys, onDone: () => table.refresh() });
      } }, icon('checkmark'), 'Record PM for listed assets'));
      if (name === 'assets' && (isAdmin() || sessionUser()?.asset_access === 'FULL')) tools.push(h('button', { class: 'btn', type: 'button', title: 'Assign every asset matching the current search and filters to an engineer, or hand over one engineer’s assets to another', onClick: async () => {
        const keys = await api.allKeys(5000);
        if (!keys.length) { toast('No assets are listed.', 'bad'); return; }
        assignEngineerDialog({ keys, onDone: () => table.refresh() });
      } }, icon('user--multiple'), 'Assign engineer'));
      if (schema?.can_create[name]) tools.push(h('button', { class: 'btn primary', type: 'button', onClick: () => newRecord({ name, label: ds.label.replace(/s$/, ''), onCreated: (id) => { table.refresh(); openDetail(id); } }) }, icon('add'), `New ${ds.label.replace(/s$/, '').toLowerCase()}`));
      let cols = await loadColumns(name, ds.columns);
      const colsBtn = h('button', { class: 'btn', type: 'button', onClick: () => columnsDialog({ name, columns: cols, onApply: (next) => { if (!next) clearSavedWidths(widthKey); cols = next || ds.columns.map((c) => ({ ...c, visible: true })); rebuildTable(); } }) }, icon('settings'), 'Columns');
      tools.push(colsBtn);
      tools.push(h('button', { class: 'btn', type: 'button', title: 'Download everything matching the current search and filters as a CSV file (opens in Excel)',
        onClick: async () => { try { const n = await downloadGet('/api/registers/' + name + '/export', paramsForServer(0, 0, false), name + '.csv'); toast(`Downloaded ${n}`); } catch (e) { toast(e.message, 'bad'); } } }, icon('download'), 'Export CSV'));
      if (name === 'assets') tools.push(h('button', { class: 'btn', type: 'button', title: 'Print Code128 barcode stickers for everything matching the current search and filters (one per asset, Asset (CI) number)',
        onClick: async () => { try { const n = await downloadGet('/api/registers/assets/labels', paramsForServer(0, 0, false), 'asset_labels.pdf'); toast(`Downloaded ${n}`); } catch (e) { toast(e.message, 'bad'); } } }, icon('barcode'), 'Print barcode labels'));
      if (!opts.embedded) holder.append(pageHead(ds.label, `${ds.label} — updates live as records change`, tools));
      facets = createFacets({ facets: ds.facets, onChange: (f, clearAll) => { filters = clearAll ? Object.fromEntries(ds.facets.map((x) => [x.key, []])) : f; applyFilters(); } });
      const myEngineerKey = name === 'engineers' ? sessionUser()?.engineer_key : null;
      const widthKey = `itam.colw.${sessionUser()?.username || sessionUser()?.id || ''}.${name}`;
      function buildTable() {
        return createTable({
          columns: cols.filter((c) => c.visible), sort: { key: sort.key, dir: sort.dir }, ariaLabel: ds.label, search: () => q, fetchPage,
          onSort: (key, dir) => { sort = { key, dir }; syncUrl(); requery(); }, onOpen: (row) => openDetail(row.id),
          widthKey,
          rowClass: myEngineerKey ? (row) => (row.id === myEngineerKey ? 'row-self' : null) : undefined,
        });
      }
      function rebuildTable() {
        const next = buildTable();
        table.el.replaceWith(next.el);
        table = next;
        requery();
      }
      table = buildTable();
      search.value = q; clearBtn.hidden = !q;
      const toolbar = h('div', { class: 'toolbar' }, h('div', { class: 'field' }, icon('search'), search, clearBtn), countEl);
      const results = h('section', { class: 'results', 'aria-label': `${ds.label} results` }, toolbar, chipsEl, table.el);
      holder.append(h('div', { class: 'register' }, facets.el, results));
      facets.setFilters(filters);
      drawChips();
      requery();
      const open = current().params.get('open');
      if (open) openDetail(open);
    } catch (e) { holder.append(errorBlock(e)); }
  })();

  const api = {
    /** Keys of the records matching the current search and filters (at most `max`) - used for bulk actions. */
    async allKeys(max = 500) {
      const out = [];
      for (let off = 0; off < max; off += 200) {
        const res = await get('/api/registers/' + name, paramsForServer(off, Math.min(200, max - off), false));
        out.push(...res.rows.map((r) => r.id));
        if (res.rows.length < 200) break;
      }
      return out;
    },
    get total() { return table?.total ?? 0; },
    onLive(ev) {
      if (!table || !ev.tables.some((t) => t === TABLE_OF[name] || t === 'portal_lock')) return;
      table.refresh();
      if (openId && rec) get(`/api/registers/${ds.detail || name}/${encodeURIComponent(openId)}`).then((d) => rec?.refresh(d)).catch(() => {});
    },
    destroy() { dead = true; drawer?.close(true); },
  };
  return api;
}
