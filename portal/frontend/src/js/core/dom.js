const SVG_NS = 'http://www.w3.org/2000/svg';

/** Tiny hyperscript helper. Attributes: class, dataset, style(object), on<Event> handlers, aria-*, booleans. Children: nodes, strings, numbers, arrays. */
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'style' && typeof v === 'object') {
        for (const [p, val] of Object.entries(v)) { if (p.startsWith('--')) el.style.setProperty(p, val); else el.style[p] = val; }
      }
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
  }
  append(el, kids);
  return el;
}

export function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

/** Carbon icon from the inline sprite (id "i-<name>"). Decorative by default. */
export function icon(name, cls = '') {
  const s = document.createElementNS(SVG_NS, 'svg');
  s.setAttribute('class', 'ico ' + cls);
  s.setAttribute('aria-hidden', 'true');
  s.setAttribute('focusable', 'false');
  const u = document.createElementNS(SVG_NS, 'use');
  u.setAttribute('href', '#i-' + name);
  s.append(u);
  return s;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function debounce(fn, ms) { let t; const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; d.cancel = () => clearTimeout(t); return d; }

/** Highlight every search token inside a string (returns nodes). */
export function hilite(text, q) {
  if (!q || text == null) return text ?? '';
  const t = String(text);
  const toks = q.toLowerCase().split(/\s+/).filter(Boolean);
  const lower = t.toLowerCase();
  const spans = [];
  for (const tok of toks) {
    let i = lower.indexOf(tok);
    while (i >= 0) { spans.push([i, i + tok.length]); i = lower.indexOf(tok, i + tok.length); }
  }
  if (!spans.length) return t;
  spans.sort((a, b) => a[0] - b[0]);
  const out = [];
  let pos = 0;
  for (const [s, e] of spans) {
    if (s < pos) continue;
    if (s > pos) out.push(t.slice(pos, s));
    out.push(h('mark', null, t.slice(s, e)));
    pos = e;
  }
  if (pos < t.length) out.push(t.slice(pos));
  return out;
}
