// Report builder: pick a register, its columns, filters, grouping and sort order. The preview updates while you work; download as Excel, CSV or PDF.
import { get, send } from '../core/api.js';
import { h, icon, debounce } from '../core/dom.js';
import { date, int } from '../core/format.js';
import { toast } from '../ui/toast.js';
import { shareMenu } from '../ui/share.js';
import { confirmBox, openModal } from '../ui/modal.js';
import { errorBlock, pageHead } from './common.js';

const OP_LABEL = { eq: 'is', neq: 'is not', in: 'is one of', contains: 'contains', starts: 'starts with', blank: 'is empty', notblank: 'is not empty', gt: 'is greater than', gte: 'is at least', lt: 'is less than', lte: 'is at most',
  between: 'is between', before: 'is before', after: 'is after', in_last_days: 'in the last … days', in_next_days: 'in the next … days' };
const PARTS = [['', 'exact value'], ['month', 'by month'], ['quarter', 'by quarter'], ['year', 'by year']];
const FN = [['count_distinct', 'distinct count of'], ['sum', 'total of'], ['avg', 'average of'], ['min', 'lowest'], ['max', 'highest']];
const PRESETS = {
  assets: [['Cover ending in 90 days', { columns: ['asset_key', 'asset_class', 'make', 'model', 'user_name', 'engineer_name', 'cover_type', 'cover_expiry_date'], filters: [{ field: 'cover_status', op: 'in', values: ['EXPIRING_90D'] }, { field: 'record_level', op: 'eq', value: 'ASSET' }], sort: [{ field: 'cover_expiry_date', dir: 'asc' }] }],
    ['Assets per engineer and class', { group_by: ['engineer_name', 'asset_class'], filters: [{ field: 'record_level', op: 'eq', value: 'ASSET' }] }],
    ['PM pending', { columns: ['asset_key', 'asset_class', 'location_code', 'engineer_name', 'pm_status'], filters: [{ field: 'pm_status', op: 'eq', value: 'PENDING' }] }]],
  calls: [['Open calls by age', { columns: ['sr_id', 'cipl_call_date', 'asset_key', 'engineer', 'priority', 'ageing_days', 'problem_description'], filters: [{ field: 'call_status', op: 'eq', value: 'OPEN' }], sort: [{ field: 'ageing_days', dir: 'desc' }] }],
    ['Calls per engineer per month', { group_by: ['engineer', 'cipl_call_date:month'] }]],
  engineers: [['Engineer workload', { columns: ['display_name', 'ecode', 'designation', 'employment_status', 'assets', 'open_calls', 'calls'] }]],
};

export function mountReports(root) {
  let dead = false, meta = null;
  const defn = { dataset: 'assets', columns: [], filters: [], match: 'all', group_by: [], metrics: [], sort: [], blank_columns: [] };
  const holder = h('div', { class: 'page' });
  root.append(holder);

  const dsSel = h('select', { id: 'rp-ds', 'aria-label': 'Register' });
  const savedSel = h('select', { id: 'rp-saved', 'aria-label': 'Saved reports' });
  const cols = h('div', { class: 'rp-cols' });
  const colFind = h('input', { type: 'search', placeholder: 'Find a column', 'aria-label': 'Find a column' });
  const filtersEl = h('div', { class: 'rp-filters' });
  const groupEl = h('div', { class: 'rp-group' });
  const blankEl = h('div', { class: 'rp-blank' });
  const sortEl = h('div', { class: 'rp-sort' });
  const matchEl = h('div', { class: 'rgroup' });
  const preview = h('div', { class: 'tbl-wrap grow' });
  const count = h('span', { class: 'count-line', 'aria-live': 'polite' });
  const status = h('div', { class: 'rec-note bad', hidden: true, role: 'alert' });
  let tools;

  const fields = () => meta.datasets[defn.dataset].fields;
  const fld = (k) => fields().find((f) => f.key === k);
  const opts = (sel, list, cur, blank) => { sel.replaceChildren(...(blank ? [h('option', { value: '' }, blank)] : []), ...list.map(([v, t]) => h('option', { value: v }, t))); sel.value = cur ?? ''; };
  const fieldOptions = (sel, cur, filter = () => true, blank) => opts(sel, fields().filter(filter).map((f) => [f.key, f.label]), cur, blank);

  const run = debounce(async () => {
    try {
      const r = await send('/api/reports/run', { definition: clean(), limit: 200 });
      if (dead) return;
      status.hidden = true;
      count.replaceChildren(h('strong', null, int(r.total)), ` ${r.total === 1 ? 'row' : 'rows'}`, r.total > 200 ? ' (first 200 shown - downloads contain all)' : '');
      preview.replaceChildren(r.rows.length ? h('table', { class: 'tbl' }, h('thead', null, h('tr', null, r.columns.map((c) => h('th', null, c.label)))),
        h('tbody', null, r.rows.map((row) => h('tr', null, r.columns.map((c) => h('td', { class: 'nowrap' }, cell(c.key, row[c.key])))))))
        : h('div', { class: 'empty' }, icon('search'), h('h3', null, 'No rows match'), h('div', null, 'Change or remove a filter.')));
    } catch (e) { status.replaceChildren(icon('error--filled'), e.message); status.hidden = false; }
  }, 260);

  function cell(key, v) {
    if (v == null || v === '') return h('span', { class: 'faint' }, '—');
    if (/(_date|^date_)/.test(key) || (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) && !/_month|_year|_quarter/.test(key))) return date(v);
    return typeof v === 'number' ? (Number.isInteger(v) && !key.endsWith('_no') ? int(v) : String(Math.round(v * 10) / 10)) : String(v);
  }

  function clean() {
    const f = defn.filters.filter((x) => x.field && (['blank', 'notblank'].includes(x.op) || x.value || x.values?.length));
    const blanks = (defn.blank_columns || []).filter((b) => (b.label || '').trim());     // a not-yet-titled blank column is left out of what actually runs
    return { ...defn, filters: f, blank_columns: blanks, columns: defn.columns.length ? defn.columns : fields().slice(0, 12).map((x) => x.key) };
  }

  const changed = () => { run(); };

  function drawColumns() {
    const q = colFind.value.trim().toLowerCase();
    cols.replaceChildren(...fields().filter((f) => !q || f.label.toLowerCase().includes(q)).map((f) => {
      const i = h('input', { type: 'checkbox', id: 'c-' + f.key }); i.checked = defn.columns.includes(f.key);
      i.addEventListener('change', () => { defn.columns = i.checked ? [...defn.columns, f.key] : defn.columns.filter((k) => k !== f.key); changed(); });
      return h('label', { class: 'opt', for: 'c-' + f.key }, i, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, f.label), h('span', { class: 'faint small' }, f.type === 'text' ? '' : f.type));
    }));
  }

  function drawFilters() {
    filtersEl.replaceChildren(...defn.filters.map((flt, idx) => {
      const f = fld(flt.field) || fields()[0];
      const fs = h('select', { 'aria-label': 'Field' }); fieldOptions(fs, flt.field);
      const os = h('select', { 'aria-label': 'Condition' }); opts(os, f.ops.map((o) => [o, OP_LABEL[o]]), flt.op);
      const v1 = h('input', { type: f.type === 'date' ? 'date' : f.type === 'number' ? 'number' : 'text', 'aria-label': 'Value', value: flt.op === 'in' ? (flt.values || []).join(', ') : (flt.value ?? ''), list: 'rp-dl' });
      const v2 = h('input', { type: f.type === 'date' ? 'date' : 'number', 'aria-label': 'Second value', value: flt.value2 ?? '' });
      const show = () => {
        const op = flt.op;
        v1.hidden = ['blank', 'notblank'].includes(op);
        v2.hidden = op !== 'between';
        v1.type = ['in_last_days', 'in_next_days'].includes(op) ? 'number' : (f.type === 'date' ? 'date' : f.type === 'number' ? 'number' : 'text');
      };
      show();
      fs.addEventListener('change', () => { const nf = fld(fs.value); flt.field = fs.value; flt.op = nf.ops[0]; flt.value = ''; flt.value2 = ''; flt.values = []; drawFilters(); changed(); });
      os.addEventListener('change', () => { flt.op = os.value; show(); changed(); });
      v1.addEventListener('input', () => { if (flt.op === 'in') flt.values = v1.value.split(',').map((s) => s.trim()).filter(Boolean); else flt.value = v1.value; changed(); });
      v1.addEventListener('focus', () => suggest(f, v1));
      v2.addEventListener('input', () => { flt.value2 = v2.value; changed(); });
      return h('div', { class: 'rp-filter' }, fs, os, v1, v2, h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Remove filter', onClick: () => { defn.filters.splice(idx, 1); drawFilters(); changed(); } }, icon('close')));
    }), h('button', { class: 'btn', type: 'button', onClick: () => { const f = fields()[0]; defn.filters.push({ field: f.key, op: f.ops[0], value: '', values: [] }); drawFilters(); } }, icon('add'), 'Add a filter'));
  }

  const suggestSeen = new Map();
  async function suggest(f, input) {
    if (f.type !== 'text') return;
    let dl = document.getElementById('rp-dl');
    if (!dl) { dl = h('datalist', { id: 'rp-dl' }); document.body.append(dl); }
    const key = defn.dataset + '.' + f.key;
    if (!suggestSeen.has(key)) suggestSeen.set(key, (await send('/api/reports/values', { dataset: defn.dataset, field: f.key })).values);
    dl.replaceChildren(...suggestSeen.get(key).map((x) => h('option', { value: x.v }, `${x.v} (${x.n})`)));
    input.setAttribute('list', 'rp-dl');
  }

  function drawGroup() {
    const gs = h('select', { 'aria-label': 'Group by' }); const part = h('select', { 'aria-label': 'Group by part' });
    const g0 = (defn.group_by[0] || '').split(':');
    fieldOptions(gs, g0[0], () => true, 'No grouping - list every record');
    opts(part, PARTS, g0[1] || '');
    const sync = () => { const f = fld(gs.value); part.hidden = !f || f.type !== 'date'; defn.group_by = gs.value ? [gs.value + (part.hidden || !part.value ? '' : ':' + part.value)] : []; };
    sync();
    gs.addEventListener('change', () => { sync(); drawGroup(); changed(); }); part.addEventListener('change', () => { sync(); changed(); });
    const second = h('select', { 'aria-label': 'Then by' }); const g1 = (defn.group_by[1] || '').split(':')[0];
    fieldOptions(second, g1, (f) => f.key !== g0[0], 'Then by… (optional)');
    second.hidden = !gs.value;
    second.addEventListener('change', () => { defn.group_by = [defn.group_by[0], ...(second.value ? [second.value] : [])]; changed(); });
    const mf = h('select', { 'aria-label': 'Summary function' }); const mfield = h('select', { 'aria-label': 'Summary field' });
    opts(mf, FN, defn.metrics[0]?.fn || 'count_distinct'); fieldOptions(mfield, defn.metrics[0]?.field, () => true, 'Add a summary column…');
    const setM = () => { defn.metrics = mfield.value ? [{ fn: mf.value, field: mfield.value }] : []; changed(); };
    mf.addEventListener('change', setM); mfield.addEventListener('change', setM);
    const metricRow = h('div', { class: 'rp-filter' }, mf, mfield); metricRow.hidden = !gs.value;
    groupEl.replaceChildren(h('div', { class: 'rp-filter' }, gs, part), h('div', { class: 'rp-filter' }, second), metricRow);
    cols.closest('.rp-sec').hidden = !!gs.value;
  }

  function drawSort() {
    const s = h('select', { 'aria-label': 'Sort by' }); fieldOptions(s, defn.sort[0]?.field, () => true, 'Default order');
    const d = h('div', { class: 'rgroup', role: 'radiogroup', 'aria-label': 'Direction' });
    const dirs = [['asc', 'Ascending'], ['desc', 'Descending']];
    dirs.forEach(([v, t]) => { const i = h('input', { type: 'radio', name: 'rp-dir', value: v }); i.checked = (defn.sort[0]?.dir || 'asc') === v; i.addEventListener('change', () => { if (defn.sort[0]) { defn.sort[0].dir = v; changed(); } }); d.append(h('label', { class: 'ropt' }, i, icon('radio-button', 'glyph off'), icon('radio-button--checked', 'glyph on'), h('span', null, t))); });
    s.addEventListener('change', () => { defn.sort = s.value ? [{ field: s.value, dir: d.querySelector('input:checked').value }] : []; changed(); });
    sortEl.replaceChildren(s, d);
  }

  function drawBlank() {
    const list = defn.blank_columns || (defn.blank_columns = []);
    blankEl.replaceChildren(...list.map((b, idx) => {
      const inp = h('input', { type: 'text', maxlength: '60', placeholder: 'Column heading, e.g. "Signature"', 'aria-label': 'Blank column heading', value: b.label || '' });
      inp.addEventListener('input', () => { b.label = inp.value; changed(); });
      return h('div', { class: 'rp-filter' }, inp, h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Remove blank column', onClick: () => { list.splice(idx, 1); drawBlank(); changed(); } }, icon('close')));
    }), h('button', { class: 'btn', type: 'button', disabled: list.length >= 10 ? true : null, onClick: () => { list.push({ label: '' }); drawBlank(); } }, icon('add'), 'Add a blank column'));
  }

  function drawMatch() {
    matchEl.replaceChildren(...[['all', 'Match all filters'], ['any', 'Match any filter']].map(([v, t]) => {
      const i = h('input', { type: 'radio', name: 'rp-match', value: v }); i.checked = defn.match === v;
      i.addEventListener('change', () => { defn.match = v; changed(); });
      return h('label', { class: 'ropt' }, i, icon('radio-button', 'glyph off'), icon('radio-button--checked', 'glyph on'), h('span', null, t));
    }));
  }

  function setDataset(name, keep) {
    defn.dataset = name;
    if (!keep) Object.assign(defn, { columns: defaultCols(name), filters: [], match: 'all', group_by: [], metrics: [], sort: [], blank_columns: [] });
    dsSel.value = name;
    drawAll();
    changed();
  }
  const defaultCols = (name) => ({ assets: ['asset_key', 'asset_class', 'make', 'model', 'serial_no', 'user_name', 'engineer_name', 'location_code', 'asset_status', 'cover_expiry_date'], calls: ['sr_id', 'cipl_call_date', 'asset_key', 'engineer', 'priority', 'call_status', 'problem_description'] }[name] || meta.datasets[name].fields.slice(0, 10).map((f) => f.key));

  function drawAll() { drawColumns(); drawFilters(); drawGroup(); drawBlank(); drawSort(); drawMatch(); drawPresets(); }

  const presetEl = h('div', { class: 'btn-row' });
  function drawPresets() {
    presetEl.replaceChildren(...(PRESETS[defn.dataset] || []).map(([label, p]) => h('button', { class: 'btn', type: 'button', onClick: () => { Object.assign(defn, { columns: [], filters: [], group_by: [], metrics: [], sort: [], match: 'all', blank_columns: [] }, JSON.parse(JSON.stringify(p))); drawAll(); changed(); } }, label)));
  }

  function drawSaved() {
    opts(savedSel, meta.saved.map((s) => [String(s.id), `${s.name}${s.shared ? '' : ' (private)'}`]), '', 'Open a saved report…');
  }
  savedSel.addEventListener('change', () => {
    const s = meta.saved.find((x) => String(x.id) === savedSel.value);
    if (!s) return;
    Object.assign(defn, { columns: [], filters: [], group_by: [], metrics: [], sort: [], match: 'all', blank_columns: [] }, JSON.parse(JSON.stringify(s.definition)));
    setDataset(defn.dataset, true);
  });

  function saveDialog() {
    const name = h('input', { id: 'sv-n', type: 'text', maxlength: '80', placeholder: 'Report name' });
    const shared = h('input', { type: 'checkbox', id: 'sv-s' });
    openModal({ title: 'Save report', lead: 'Saving again with the same name replaces your earlier version.',
      body: h('div', null, h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'sv-n' }, 'Name'), name), h('label', { class: 'opt', for: 'sv-s' }, shared, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, 'Share with everyone who can sign in'))),
      actions: [{ label: 'Cancel' }, { label: 'Save', primary: true, icon: 'save', onClick: async () => { const r = await send('/api/reports/save', { name: name.value, definition: clean(), shared: shared.checked }); meta.saved = r.saved; drawSaved(); toast('Report saved'); } }] });
  }
  async function deleteSaved() {
    const s = meta.saved.find((x) => String(x.id) === savedSel.value);
    if (!s) { toast('Open a saved report first.'); return; }
    if (!(await confirmBox({ title: 'Delete saved report', lead: `“${s.name}” will be removed. The data it reads is not affected.`, confirm: 'Delete', danger: true }))) return;
    try { const r = await send('/api/reports/delete', { id: s.id }); meta.saved = r.saved; drawSaved(); toast('Deleted'); } catch (e) { toast(e.message, 'bad'); }
  }

  (async () => {
    try {
      meta = await get('/api/reports/meta');
      if (dead) return;
      opts(dsSel, Object.entries(meta.datasets).map(([k, v]) => [k, v.label]), 'assets');
      dsSel.addEventListener('change', () => setDataset(dsSel.value));
      colFind.addEventListener('input', drawColumns);
      tools = [shareMenu({ pack: null, formats: ['xlsx', 'csv', 'pdf'], endpoint: '/api/reports/export', body: () => ({ definition: clean() }), mail: false }),
        h('button', { class: 'btn', type: 'button', onClick: saveDialog }, icon('save'), 'Save…')];
      holder.append(pageHead('Report builder', 'Choose what to list, how to filter and group it. The preview updates as you change anything.', tools));
      const sec = (title, ...kids) => h('section', { class: 'rp-sec' }, h('h3', null, title), ...kids);
      const colsSec = sec('Columns', colFind, h('div', { class: 'btn-row tight' }, h('button', { class: 'link-btn', type: 'button', onClick: () => { defn.columns = fields().map((f) => f.key); drawColumns(); changed(); } }, 'All'), h('button', { class: 'link-btn', type: 'button', onClick: () => { defn.columns = []; drawColumns(); changed(); } }, 'None')), cols);
      const side = h('aside', { class: 'rp-side' },
        h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'rp-ds' }, 'Register'), dsSel), h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'rp-saved' }, 'Saved reports'), h('div', { class: 'row-inline-btn' }, savedSel, h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Delete the selected saved report', onClick: deleteSaved }, icon('trash-can')))),
        sec('Ready-made', presetEl), sec('Filters', filtersEl, matchEl), sec('Group and summarise', groupEl), colsSec,
        sec('Blank columns', h('p', { class: 'muted small' }, 'Added at the end, left empty for the reader to fill in by hand.'), blankEl), sec('Sort', sortEl));
      holder.append(h('div', { class: 'rp' }, side, h('section', { class: 'results' }, h('div', { class: 'toolbar' }, h('strong', null, 'Preview'), count), status, preview)));
      drawSaved();
      setDataset('assets');
    } catch (e) { holder.append(errorBlock(e)); }
  })();
  return { destroy() { dead = true; document.getElementById('rp-dl')?.remove(); }, onLive: () => run() };
}

