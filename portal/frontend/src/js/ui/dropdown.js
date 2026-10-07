// One dropdown look for the whole portal. A native <select> or <input list> opens an operating-system popup that cannot be styled and
// differs between the two; this replaces that popup - and only the popup - with the portal's own listbox. The real control stays in
// the page as the trigger (so labels, validation, events, `.value`, disabled state and every existing style keep working), the
// browser's own popup is suppressed, and a choice is written back to it with ordinary input/change events.
// Opt out with data-native on a control. Multi-selects keep the native control (a popup does not suit them).
import { h } from '../core/dom.js';

let pop = null;          // the open popup element
let ctx = null;          // { el, kind, items, active, render, mo }
let uid = 0;

const isSelect = (el) => el?.tagName === 'SELECT' && !el.multiple && !el.hasAttribute('data-native') && !el.disabled;
const listId = (el) => el?.tagName === 'INPUT' && (el.getAttribute('list') || el.dataset.ddList);
const isListInput = (el) => !!listId(el) && !el.hasAttribute('data-native') && !el.disabled && !el.readOnly;

function selectItems(sel) {
  const out = [];
  const add = (o) => out.push({ value: o.value, text: o.textContent, disabled: o.disabled, selected: o.value === sel.value && !o.disabled });
  for (const c of sel.children) {
    if (c.tagName === 'OPTGROUP') { out.push({ group: c.label }); [...c.children].forEach(add); } else add(c);
  }
  return out;
}

function listItems(input, query) {
  const dl = document.getElementById(input.dataset.ddList);
  if (!dl) return [];
  const q = query.trim().toLowerCase();
  const all = [...dl.options].map((o) => ({ value: o.value, text: o.textContent && o.textContent !== o.value ? o.textContent : '' }));
  const exact = all.some((o) => o.value.toLowerCase() === q);
  const hit = !q || exact ? all : all.filter((o) => o.value.toLowerCase().includes(q) || o.text.toLowerCase().includes(q));
  return hit.slice(0, 120).map((o) => ({ ...o, selected: o.value.toLowerCase() === q }));
}

function close() {
  if (!pop) return;
  ctx.mo?.disconnect();
  ctx.el.setAttribute('aria-expanded', 'false');
  ctx.el.removeAttribute('aria-activedescendant');
  pop.remove();
  pop = ctx = null;
}

function place() {
  const r = ctx.el.getBoundingClientRect();
  pop.style.minWidth = Math.max(r.width, 160) + 'px';
  pop.style.left = Math.max(8, Math.min(r.left, innerWidth - pop.offsetWidth - 8)) + 'px';
  const below = innerHeight - r.bottom - 12, above = r.top - 12;
  const want = Math.min(pop.scrollHeight, 288);
  const up = below < want && above > below;
  pop.style.maxHeight = Math.max(120, Math.min(288, up ? above : below)) + 'px';
  pop.style.top = up ? Math.max(8, r.top - 4 - pop.offsetHeight) + 'px' : r.bottom + 4 + 'px';
}

function render() {
  const rows = ctx.items;
  pop.replaceChildren();
  const opts = [];
  rows.forEach((it) => {
    if (it.group != null) { pop.append(h('div', { class: 'dd-group', role: 'presentation' }, it.group)); return; }
    const node = h('div', { class: 'dd-opt', role: 'option', id: `dd${uid}-${opts.length}`, 'aria-selected': it.selected ? 'true' : 'false', 'aria-disabled': it.disabled ? 'true' : null },
      h('span', { class: 'dd-t' }, it.text || it.value || '—'), it.text && ctx.kind === 'input' ? h('span', { class: 'dd-s' }, it.value) : null);
    if (!it.value && ctx.kind === 'select') node.classList.add('dd-blank');
    if (!it.disabled) node.addEventListener('mousedown', (e) => { e.preventDefault(); choose(it); });
    node.addEventListener('mousemove', () => { if (!it.disabled) setActive(opts.indexOf(node)); });
    opts.push(node);
    pop.append(node);
  });
  if (!opts.length) pop.append(h('div', { class: 'dd-empty' }, 'No matches'));
  ctx.nodes = opts;
  ctx.rows = rows.filter((r) => r.group == null);
  const sel = ctx.rows.findIndex((r) => r.selected);
  setActive(sel >= 0 ? sel : ctx.rows.findIndex((r) => !r.disabled), true);
  place();
}

function setActive(i, scroll) {
  if (!ctx) return;
  ctx.active = i;
  ctx.nodes.forEach((n, k) => n.classList.toggle('active', k === i));
  const n = ctx.nodes[i];
  if (n) { ctx.el.setAttribute('aria-activedescendant', n.id); if (scroll !== false) n.scrollIntoView({ block: 'nearest' }); }
}

function move(step) {
  const rows = ctx.rows;
  if (!rows.length) return;
  let i = ctx.active;
  for (let n = 0; n < rows.length; n++) { i = (i + step + rows.length) % rows.length; if (!rows[i].disabled) break; }
  setActive(i);
}

function choose(it) {
  const el = ctx.el;
  const kind = ctx.kind;
  close();
  if (kind === 'select') { if (el.value === it.value) { el.focus(); return; } el.value = it.value; } else el.value = it.value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.focus();
}

function open(el, kind) {
  if (ctx?.el === el) return;
  close();
  uid += 1;
  pop = h('div', { class: 'dd-pop', role: 'listbox', id: `dd${uid}` });
  ctx = { el, kind, items: [], nodes: [], rows: [], active: -1, mo: null };
  document.body.append(pop);
  el.setAttribute('aria-expanded', 'true');
  el.setAttribute('aria-controls', pop.id);
  refresh();
  if (kind === 'input') {              // suggestions that arrive later (a server lookup) re-render the open list
    const dl = document.getElementById(el.dataset.ddList);
    if (dl) { ctx.mo = new MutationObserver(() => refresh()); ctx.mo.observe(dl, { childList: true }); }
  }
}

function refresh() {
  if (!ctx) return;
  ctx.items = ctx.kind === 'select' ? selectItems(ctx.el) : listItems(ctx.el, ctx.el.value);
  render();
}

// -------------------------------------------------------------------------- wiring (delegated, so views never need to know)
document.addEventListener('mousedown', (e) => {
  if (pop && !pop.contains(e.target) && e.target !== ctx.el) close();
  const el = e.target.closest?.('select');
  if (!isSelect(el) || e.button !== 0) return;
  e.preventDefault();                  // stops the native popup
  el.focus();
  if (ctx?.el === el) close(); else open(el, 'select');
}, true);

document.addEventListener('focusin', (e) => {
  const el = e.target;
  if (el.tagName === 'INPUT' && el.hasAttribute('list') && !el.hasAttribute('data-native')) {
    el.dataset.ddList = el.getAttribute('list');
    el.removeAttribute('list');        // no native datalist popup; the options stay in the page for us to read
    el.setAttribute('role', 'combobox');
    el.setAttribute('aria-autocomplete', 'list');
  }
  if (isListInput(el)) open(el, 'input');
  else if (pop && el !== ctx.el && !pop.contains(el)) close();
});

document.addEventListener('click', (e) => {          // clicking a focused list input again (after Esc) reopens it
  const el = e.target;
  if (isListInput(el) && ctx?.el !== el) open(el, 'input');
});

document.addEventListener('input', (e) => {
  if (ctx?.kind === 'input' && e.target === ctx.el && e.isTrusted) refresh();
  else if (isListInput(e.target) && e.isTrusted && !ctx) open(e.target, 'input');
});

document.addEventListener('keydown', (e) => {
  const el = e.target;
  if (!ctx) {
    if (isSelect(el) && (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ' || (e.altKey && e.key === 'ArrowDown'))) { e.preventDefault(); open(el, 'select'); }
    else if (isListInput(el) && e.key === 'ArrowDown') { e.preventDefault(); open(el, 'input'); }
    return;
  }
  if (el !== ctx.el) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
  else if (e.key === 'Home' && ctx.kind === 'select') { e.preventDefault(); setActive(ctx.rows.findIndex((r) => !r.disabled)); }
  else if (e.key === 'End' && ctx.kind === 'select') { e.preventDefault(); setActive(ctx.rows.length - 1 - [...ctx.rows].reverse().findIndex((r) => !r.disabled)); }
  else if (e.key === 'Enter' || (e.key === ' ' && ctx.kind === 'select')) {
    const it = ctx.rows[ctx.active];
    if (it && !it.disabled) { e.preventDefault(); choose(it); } else if (ctx.kind === 'select') e.preventDefault();
    else close();
  } else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
  else if (e.key === 'Tab') close();
  else if (ctx.kind === 'select' && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {      // type-ahead
    const k = e.key.toLowerCase();
    const start = ctx.active + 1;
    const order = [...ctx.rows.keys()];
    const hit = [...order.slice(start), ...order.slice(0, start)].find((i) => !ctx.rows[i].disabled && ctx.rows[i].text.toLowerCase().startsWith(k));
    if (hit != null) setActive(hit);
  }
}, true);

addEventListener('resize', close);
addEventListener('scroll', (e) => { if (pop && !pop.contains(e.target)) close(); }, true);
