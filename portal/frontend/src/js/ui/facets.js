// Live facet panel: checkbox / radio options drawn with Carbon glyphs, counts that follow every other active filter, no Apply button.
import { h, icon } from '../core/dom.js';
import { int } from '../core/format.js';
import { genderGlyph, labelOf } from './badge.js';

export const BLANK = '~blank';
const RAW = new Set(['location_code', 'dq_flags', 'month', 'year', 'age_bucket', 'device_model', 'make']);
const NAMES = new Set(['engineer_name', 'engineer', 'received_by']);
const GOOD = new Set(['Received', 'Recorded', 'Sent', 'Linked']);
const BAD = new Set(['Pending', 'Missing', 'Not sent', 'Not linked']);
const titleCase = (s) => String(s).toLowerCase().replace(/(^|\s)\S/g, (m) => m.toUpperCase());

export function optionLabel(key, v, labels = {}) {
  if (v === BLANK) return '(blank)';
  if (labels[v]) return labels[v];
  if (RAW.has(key)) return v;
  if (NAMES.has(key)) return titleCase(v);
  return labelOf(v);
}

export function createFacets({ facets, onChange }) {
  let filters = {};
  let counts = {};
  const open = new Map(JSON.parse(localStorage.getItem('itam.facets.open') || '[]'));
  const finds = new Map();
  const more = new Set();

  const el = h('aside', { class: 'facets', 'aria-label': 'Filters' });

  const persist = () => { try { localStorage.setItem('itam.facets.open', JSON.stringify([...open])); } catch (_) { /* ignore */ } };
  const selectedCount = () => Object.values(filters).reduce((n, v) => n + v.length, 0);

  function toggle(f, v, checked) {
    const cur = new Set(filters[f.key] || []);
    if (f.kind === 'radio') { filters = { ...filters, [f.key]: checked ? [v] : [] }; }
    else { checked ? cur.add(v) : cur.delete(v); filters = { ...filters, [f.key]: [...cur] }; }
    onChange(filters);
  }

  function optionEl(f, item, selected) {
    const isRadio = f.kind === 'radio';
    const id = `fo-${f.key}-${item.v}`;
    const input = h('input', { type: isRadio ? 'radio' : 'checkbox', name: 'r-' + f.key, id, 'data-f': f.key, 'data-v': item.v });
    input.checked = selected;
    input.addEventListener('change', () => toggle(f, item.v, input.checked));
    if (isRadio) input.addEventListener('click', () => { if (selected && input.checked) { /* clicking the chosen radio again keeps it */ } });
    const label = optionLabel(f.key, item.v, f.labels);
    const state = f.kind === 'gender' ? genderGlyph(item.v) : f.kind === 'bool' ? (GOOD.has(item.v) ? h('span', { class: 'state-ico tick' }, icon('checkmark--filled')) : BAD.has(item.v) ? h('span', { class: 'state-ico cross' }, icon('close--filled')) : null) : null;
    return h('label', { class: 'opt', for: id, 'data-zero': item.n === 0 ? 'true' : null },
      input,
      icon(isRadio ? 'radio-button' : 'checkbox', 'glyph off'), icon(isRadio ? 'radio-button--checked' : 'checkbox--checked--filled', 'glyph on'),
      state, h('span', { class: 'name', title: label }, label), h('span', { class: 'n' }, int(item.n)));
  }

  function facetEl(f) {
    const sel = filters[f.key] || [];
    const items = counts[f.key] || [];
    const isOpen = open.has(f.key) ? open.get(f.key) : true;
    const body = h('div', { class: 'f-b' });
    if (f.kind === 'radio') {
      const allSel = sel.length === 0;
      const allInput = h('input', { type: 'radio', name: 'r-' + f.key, id: `fo-${f.key}-all`, 'data-f': f.key, 'data-v': '' });
      allInput.checked = allSel;
      allInput.addEventListener('change', () => toggle(f, '', false));
      const total = items.reduce((n, i) => n + i.n, 0);
      body.append(h('label', { class: 'opt', for: `fo-${f.key}-all` }, allInput, icon('radio-button', 'glyph off'), icon('radio-button--checked', 'glyph on'), h('span', { class: 'name' }, 'All'), h('span', { class: 'n' }, int(total))));
    }
    const q = (finds.get(f.key) || '').toLowerCase();
    let shown = items.filter((i) => !q || optionLabel(f.key, i.v, f.labels).toLowerCase().includes(q));
    const limit = 10;
    const expanded = more.has(f.key) || q;
    if (!expanded && shown.length > limit) shown = shown.filter((i, idx) => idx < limit || sel.includes(i.v));
    if (items.length > 12) {
      const input = h('input', { type: 'search', placeholder: `Find in ${f.label.toLowerCase()}`, 'aria-label': `Find in ${f.label}`, 'data-find': f.key });
      input.value = finds.get(f.key) || '';
      input.addEventListener('input', () => { finds.set(f.key, input.value); redraw(f.key, input); });
      body.append(h('div', { class: 'f-find' }, input));
    }
    shown.forEach((i) => body.append(optionEl(f, i, sel.includes(i.v))));
    if (!q && items.length > limit && shown.length < items.length) {
      body.append(h('button', { class: 'link-btn', type: 'button', style: { margin: '4px 8px' }, onClick: () => { more.add(f.key); redraw(f.key); } }, `Show all ${items.length}`));
    } else if (more.has(f.key) && items.length > limit) {
      body.append(h('button', { class: 'link-btn', type: 'button', style: { margin: '4px 8px' }, onClick: () => { more.delete(f.key); redraw(f.key); } }, 'Show fewer'));
    }
    const head = h('button', { class: 'f-h', type: 'button', 'aria-expanded': String(isOpen), onClick: () => { open.set(f.key, !(open.has(f.key) ? open.get(f.key) : true)); persist(); redraw(f.key); } },
      h('span', null, f.label, sel.length && f.kind !== 'radio' ? h('span', { class: 'faint', style: { fontWeight: 400 } }, ` · ${sel.length}`) : null), icon('chevron--down', 'sm'));
    return h('section', { class: 'facet', 'data-key': f.key, 'data-open': String(isOpen) }, head, body);
  }

  function redraw(key, keepFocus) {
    const old = el.querySelector(`.facet[data-key="${key}"]`);
    const f = facets.find((x) => x.key === key);
    const focusSel = keepFocus ? null : document.activeElement?.dataset?.f === key ? `input[data-f="${key}"][data-v="${document.activeElement.dataset.v}"]` : null;
    const fresh = facetEl(f);
    old.replaceWith(fresh);
    if (keepFocus) { const i = fresh.querySelector('input[data-find]'); if (i) { i.focus({ preventScroll: true }); i.setSelectionRange(i.value.length, i.value.length); } }
    if (focusSel) fresh.querySelector(focusSel)?.focus({ preventScroll: true });
  }

  function draw() {
    const a = document.activeElement;
    const restore = a && el.contains(a) ? { f: a.dataset.f, v: a.dataset.v, find: a.dataset.find } : null;
    const n = selectedCount();
    const head = h('div', { class: 'facets-h' }, h('h2', null, 'Narrow down results', n ? h('span', { class: 'count' }, `${n} filter${n === 1 ? '' : 's'} applied`) : null),
      h('button', { class: 'link-btn', type: 'button', disabled: n ? null : true, style: { opacity: n ? 1 : 0.4 }, onClick: () => { filters = Object.fromEntries(facets.map((x) => [x.key, x.kind === 'radio' ? [] : []])); onChange(filters, true); } }, 'Clear all'));
    el.replaceChildren(head, ...facets.map(facetEl));
    if (restore) {
      const t = restore.find
        ? el.querySelector(`input[data-find="${restore.find}"]`)
        : restore.f ? el.querySelector(`input[data-f="${restore.f}"][data-v="${CSS.escape(restore.v ?? '')}"]`) : null;
      if (t) { t.focus({ preventScroll: true }); if (restore.find) t.setSelectionRange(t.value.length, t.value.length); }
    }
  }

  return {
    el,
    setFilters(f) { filters = f; draw(); },
    setCounts(c) { counts = c; draw(); },
    update(c, f) { counts = c; filters = f; draw(); },
  };
}
