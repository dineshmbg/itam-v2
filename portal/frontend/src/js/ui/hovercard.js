// Hover cards: pointing at (or focusing) an asset, SR ID, CPF number or ECODE anywhere in the portal opens a floating window with every available detail.
import { get } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { date, dec, int, label as fieldLabel, person } from '../core/format.js';
import { href } from '../core/router.js';
import { badge } from './badge.js';

const BADGE_KEYS = new Set(['asset_status', 'cover_status', 'pm_status', 'call_status', 'priority', 'spare_status', 'return_status', 'faulty_spare_status', 'employment_status', 'onboarding_status', 'record_status']);

/** An interactive reference: shows a card on hover/focus and links to the record. kind: asset | call | cpf | ecode | engineer */
export function entity(kind, id, text, { mono = true, ...extra } = {}) {
  if (id == null || id === '') return h('span', { class: 'faint' }, '—');
  const target = { asset: ['registers/assets', 'open'], call: ['registers/calls', 'open'] }[kind];
  const a = h('a', { class: 'ent' + (mono ? ' mono' : ''), href: target ? href(target[0], { [target[1]]: id }) : '#/dashboard/engineers?eng=' + encodeURIComponent(id), 'data-card': kind, 'data-id': String(id), ...extra }, text ?? String(id));
  if (kind === 'cpf') a.href = href('registers/assets', { q: String(id) });
  return a;
}

const cache = new Map();
let el = null, timer = null, hideTimer = null, owner = null;

function fmt(key, v) {
  if (v == null || v === '') return h('span', { class: 'faint' }, '—');
  if (BADGE_KEYS.has(key)) return badge(v, key);
  if (key.endsWith('_date') || key.startsWith('date_')) return date(v);
  if (typeof v === 'number') return key.endsWith('_no') || key === 'cpf_no' ? String(v) : (Number.isInteger(v) ? int(v) : dec(v));
  return /email/.test(key) ? h('span', { class: 'email' }, v) : String(v);
}

/** The barcode is generated server-side (offline, no client-side library) - the SVG markup is trusted, it comes from this portal's own API. */
function barcodeBlock(svg) {
  const box = h('div', { class: 'hc-barcode' });
  box.innerHTML = svg;
  return box;
}

function build(d) {
  const rows = Object.entries(d.row || {});
  const x = d.extra || {};
  const counts = Object.entries(x).filter(([, v]) => typeof v === 'number');
  const card = h('div', { class: 'hcard', role: 'dialog', 'aria-label': `${d.kind} details` },
    h('div', { class: 'hc-h' }, h('div', { class: 'hc-t' }, h('strong', null, d.kind === 'engineer' || d.kind === 'cpf' ? person(d.title) : d.title), d.archived ? h('span', { class: 'badge mute' }, 'Archived') : null),
      h('div', { class: 'muted' }, d.subtitle || '')),
    counts.length ? h('div', { class: 'hc-counts' }, counts.map(([k, v]) => h('span', null, h('b', null, int(v)), ' ', fieldLabel(k).toLowerCase()))) : null,
    h('div', { class: 'hc-b' }, h('div', { class: 'kv compact' }, rows.flatMap(([k, v]) => [h('div', { class: 'k' }, fieldLabel(k)), h('div', { class: 'v' }, fmt(k, v))]))),
    x.checklist?.length ? h('div', { class: 'hc-check' }, x.checklist.map((c) => h('span', { class: c.status === 'PENDING' ? 'cross' : 'tick', title: `${fieldLabel(c.item)}: ${c.status.toLowerCase()}` }, icon(c.status === 'PENDING' ? 'close--filled' : 'checkmark--filled'), fieldLabel(c.item)))) : null,
    x.asset ? h('div', { class: 'hc-check' }, h('span', { class: 'muted' }, 'Asset: '), h('a', { class: 'mono', href: href('registers/assets', { open: x.asset.asset_key }) }, x.asset.asset_key), ` · ${[x.asset.make, x.asset.model].filter(Boolean).join(' ')}`) : null,
    d.kind === 'asset' && d.barcode_svg ? barcodeBlock(d.barcode_svg) : null,
    d.link ? h('div', { class: 'hc-f' }, h('a', { href: href(d.link.path, Object.fromEntries(Object.entries(d.link).filter(([k]) => k !== 'path'))) }, 'Open ', icon('arrow--right'))) : null);
  card.addEventListener('pointerenter', () => clearTimeout(hideTimer));
  card.addEventListener('pointerleave', scheduleHide);
  return card;
}

function place(target) {
  const r = target.getBoundingClientRect();
  const w = Math.min(420, window.innerWidth - 16);
  el.style.width = w + 'px';
  let left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
  el.style.left = left + 'px';
  const h2 = el.offsetHeight;
  const below = window.innerHeight - r.bottom;
  el.style.top = (below >= Math.min(h2, 320) + 12 || below > r.top ? Math.min(r.bottom + 6, window.innerHeight - h2 - 8) : Math.max(8, r.top - h2 - 6)) + 'px';
  el.style.maxHeight = Math.max(200, window.innerHeight - 24) + 'px';
}

async function show(target) {
  const kind = target.dataset.card, id = target.dataset.id;
  const key = kind + '/' + id;
  owner = target;
  let d = cache.get(key);
  if (!d || Date.now() - d.at > 60000) {
    hide(true);
    el = h('div', { class: 'hcard loading', role: 'status' }, 'Loading…');
    document.body.append(el);
    place(target);
    try { d = { at: Date.now(), data: await get(`/api/card/${kind}/${encodeURIComponent(id)}`) }; cache.set(key, d); }
    catch (e) {
      if (owner !== target) return;
      el.replaceChildren(h('span', { class: 'muted' }, e.status === 404 ? 'No details on record.' : 'Details are not available right now.'));
      return;
    }
  }
  if (owner !== target) return;
  hide(true);
  el = build(d.data);
  document.body.append(el);
  place(target);
}

function hide(now) {
  clearTimeout(timer); clearTimeout(hideTimer);
  if (now) { el?.remove(); el = null; }
}
function scheduleHide() { clearTimeout(hideTimer); hideTimer = setTimeout(() => { hide(true); owner = null; }, 220); }

export function initHoverCards() {
  const find = (e) => (e.target instanceof Element ? e.target.closest('[data-card]') : null);
  document.addEventListener('pointerover', (e) => {
    if (e.pointerType === 'touch') return;
    const t = find(e);
    if (!t || t === owner && el) { if (t) clearTimeout(hideTimer); return; }
    clearTimeout(timer); clearTimeout(hideTimer);
    timer = setTimeout(() => show(t), 320);
  });
  document.addEventListener('pointerout', (e) => {
    const t = find(e);
    if (!t) return;
    clearTimeout(timer);
    if (!(e.relatedTarget instanceof Element && e.relatedTarget.closest('.hcard'))) scheduleHide();
  });
  document.addEventListener('focusin', (e) => { const t = find(e); if (t) show(t); });
  document.addEventListener('focusout', (e) => { if (find(e)) scheduleHide(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && el) { hide(true); owner = null; } });
  document.addEventListener('scroll', (e) => { if (el && !(e.target.closest && e.target.closest('.hcard'))) { hide(true); owner = null; } }, true);
  // touch: a first tap opens the card, a second tap follows the link
  document.addEventListener('click', (e) => {
    const t = find(e);
    if (t && e.pointerType === 'touch' && owner !== t) { e.preventDefault(); show(t); }
    else if (el && !e.target.closest('.hcard') && !t) { hide(true); owner = null; }
  }, true);
}
