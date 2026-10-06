import { h, icon } from '../core/dom.js';
import { int } from '../core/format.js';
import { href } from '../core/router.js';
import { colorVar } from '../ui/charts.js';

export function pageHead(title, sub, right) {
  return h('div', { class: 'page-head' }, h('div', null, h('h1', null, title), sub ? h('div', { class: 'sub', 'data-sub': '' }, sub) : null), right ? h('div', { class: 'page-tools' }, right) : null);
}

export function panel(title, { hint, cls = '', flush = false } = {}, ...body) {
  return h('section', { class: 'panel ' + cls }, h('div', { class: 'panel-h' }, h('h2', null, title), hint ? h('span', { class: 'hint' }, hint) : null), h('div', { class: 'panel-b' + (flush ? ' flush' : '') }, body));
}

/** Flat KPI strip. items: {id, label, value, sub, tone, href, ico} */
export function kpiStrip(items) {
  const el = h('div', { class: 'kpis', role: 'group', 'aria-label': 'Key figures' });
  items.forEach((k) => el.append(kpiCell(k)));
  return el;
}
// Whole-number figures count up to their value (first paint) or from the old value (live refresh); anything else (percentages, text) is set as is.
const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function countTo(el, from, to, text) {
  if (reduced() || !Number.isFinite(to) || from === to) { el.textContent = text; return; }
  cancelAnimationFrame(el._raf);
  const t0 = performance.now(), dur = Math.min(600, 250 + Math.abs(to - from));
  const step = (t) => {
    const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 4);
    el.textContent = p < 1 ? int(Math.round(from + (to - from) * e)) : text;
    if (p < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}
const asInt = (v) => (/^\d[\d,]*$/.test(String(v)) ? Number(String(v).replace(/,/g, '')) : NaN);

export function kpiCell(k) {
  const tag = k.href ? 'a' : 'div';
  const c = h(tag, { class: 'kpi', 'data-id': k.id, 'data-tone': k.tone || null, href: k.href || null },
    h('span', { class: 'kpi-l' }, k.ico ? icon(k.ico) : null, k.label), h('span', { class: 'kpi-v' }, k.value), h('span', { class: 'kpi-s' }, k.sub || ''));
  c.dataset.raw = String(k.value);
  const n = asInt(k.value);
  if (Number.isFinite(n)) { const v = c.querySelector('.kpi-v'); v.textContent = int(0); countTo(v, 0, n, String(k.value)); }
  return c;
}
/** Update KPI cells in place and flash the ones whose value changed (live refresh). */
export function kpiUpdate(strip, items) {
  items.forEach((k) => {
    const c = strip.querySelector(`[data-id="${k.id}"]`);
    if (!c) return;
    const changed = c.dataset.raw !== String(k.value);
    const v = c.querySelector('.kpi-v'), from = asInt(c.dataset.raw), to = asInt(k.value);
    if (changed && Number.isFinite(from) && Number.isFinite(to)) countTo(v, from, to, String(k.value)); else v.textContent = k.value;
    c.querySelector('.kpi-s').textContent = k.sub || '';
    c.dataset.raw = String(k.value);
    if (k.tone) c.dataset.tone = k.tone; else delete c.dataset.tone;
    if (changed) { c.classList.remove('changed'); void c.offsetWidth; c.classList.add('changed'); }
  });
}

export function legend(items, hrefFn) {
  return h('div', { class: 'legend' }, items.map((it, i) => {
    const inner = [h('i', { class: 'sw', style: { background: it.color || colorVar(i) } }), it.label, it.n != null ? h('b', null, int(it.n)) : null];
    return hrefFn ? h('a', { href: hrefFn(it, i) }, inner) : h('span', null, inner);
  }));
}

/** Compact horizontal bars (accessible list, each row can be a link). rows: {label, n, href?} */
export function barRows(rows, { max, tone } = {}) {
  const m = max || Math.max(1, ...rows.map((r) => r.n));
  return h('div', { class: 'bars' }, rows.map((r) => {
    const tag = r.href ? 'a' : 'div';
    return h(tag, { class: 'bar-row', href: r.href || null }, h('span', { class: 'bar-l', title: r.label }, r.label),
      h('span', { class: 'bar-t' }, h('i', { style: { width: (100 * r.n / m) + '%', background: r.color || (tone ? `var(--c-${tone})` : null) } })), h('span', { class: 'bar-n' }, int(r.n)));
  }));
}

export const regHref = (ds, params) => href('registers/' + ds, params);

export function errorBlock(e, retry) {
  return h('div', { class: 'err', role: 'alert' }, icon('error--filled', 'lg'), h('div', null, h('strong', null, 'Could not load this view. '), e.message || 'Unknown error', ' ',
    retry ? h('button', { class: 'link-btn', type: 'button', onClick: retry }, 'Try again') : null));
}

export const canvas = (label, cls = '') => h('div', { class: 'chart-box ' + cls }, h('canvas', { role: 'img', 'aria-label': label }));

export const loading = () => h('div', { class: 'loading-line', role: 'status' }, 'Loading…');

export function updatedNote() { return h('span', { class: 'updated', 'data-updated': '' }, ''); }
