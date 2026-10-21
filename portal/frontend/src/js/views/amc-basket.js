// Remove from AMC for administrators and Team Leader/SI: the Assets register itself, in "pick" mode (same facets, search, grid and columns as every
// register). A click puts a row in the basket; the basket is a side window with the request form. It survives changing the filters, so assets of
// any class and type go in one request. The server checks every asset again - see portal/app/amcremoval.py.
import { downloadGet, get, send } from '../core/api.js';
import { debounce, h, icon } from '../core/dom.js';
import { href } from '../core/router.js';
import { int, titleCase } from '../core/format.js';
import { user as sessionUser } from '../core/session.js';
import { toast } from '../ui/toast.js';
import { openDrawer } from '../ui/drawer.js';
import { openModal } from '../ui/modal.js';
import { listChecker } from './amc-list.js';
import { mountRegister } from './register.js';

const inr = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const label = (s) => titleCase(String(s || '').replace(/_/g, ' '));
let seq = 0;

// ------------------------------------------------------------------ the basket (lives while the portal is open)
const basket = new Map();                                          // asset_key -> { row, reason }
const subs = new Set();
const changed = () => subs.forEach((f) => f());
export const inBasket = (key) => basket.has(key);

/** Add rows that the server has already checked. `reason` is the default for rows that came without one (a filled-in template brings its own). */
export function addRows(rows, reason = '') {
  rows.forEach((r) => { if (!basket.has(r.asset_key)) basket.set(r.asset_key, { row: r, reason: r.reason || reason }); });
  changed();
}

/** Asset keys from the register (or anywhere): the server says which can be removed; those go in the basket, the rest are explained. */
export async function addKeys(keys) {
  if (!keys.length) { toast('No assets are listed.', 'bad'); return; }
  const res = await send('/api/amc-removal/match', { tokens: keys });
  const ok = res.rows.filter((r) => r.result === 'OK' && !basket.has(r.row.asset_key));
  addRows(ok.map((r) => r.row));
  const skipped = res.rows.filter((r) => r.result !== 'OK');
  const why = {};
  skipped.forEach((r) => { why[r.why] = (why[r.why] || 0) + 1; });
  const detail = Object.entries(why).slice(0, 3).map(([w, n]) => `${w} (${n})`).join('; ');
  toast(skipped.length ? `${ok.length} added to the basket. ${skipped.length} not added: ${detail}.` : `${ok.length} added to the basket.`, skipped.length && !ok.length ? 'bad' : undefined);
}

async function toggle(row) {
  if (basket.has(row.id)) { basket.delete(row.id); changed(); return; }
  try { await addKeys([row.id]); } catch (e) { toast(e.message, 'bad'); }
}

// ------------------------------------------------------------------ the basket window
let metaP = null;
const getMeta = () => (metaP ||= get('/api/amc-removal/meta').catch((e) => { metaP = null; throw e; }));
const frow = (id, text, control, hint) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, text), control, hint ? h('div', { class: 'muted small' }, hint) : null);

async function openBasket({ onDone }) {
  let meta;
  try { meta = await getMeta(); } catch (e) { toast(e.message, 'bad'); return; }
  const drawer = openDrawer({ wide: true, title: 'Basket', subtitle: 'Assets to remove from the AMC, in one request' });
  const id = (n) => `amcb-${n}-${++seq}`;
  const reasonOpts = () => meta.reasons.map((r) => h('option', { value: r.value }, r.label));
  const allReason = h('select', { id: id('all') }, h('option', { value: '' }, 'Choose a reason'), reasonOpts());
  const when = h('input', { id: id('when'), type: 'date', value: meta.default_date, min: meta.quarter.start, max: meta.quarter.end });
  const me = sessionUser()?.username;
  const who = h('select', { id: id('who') }, h('option', { value: '' }, 'Choose who approved'), meta.approvers.map((a) => h('option', { value: a.username }, `${a.display_name} (${a.username})`)));
  if (me && meta.approvers.some((a) => a.username === me)) who.value = me;           // the person at the keyboard is usually the one who approved
  const note = h('input', { id: id('note'), type: 'text', maxlength: '300', placeholder: 'Optional' });
  const override = h('input', { id: id('ovr'), type: 'text', maxlength: '300', placeholder: 'Why is it still right to remove assets with open calls?' });
  const overrideRow = frow(override.id, 'Override note (required)', override);
  const credit = h('div', { class: 'rec-note info', role: 'status' });
  const list = h('div', { class: 'tbl-wrap' });
  const submit = h('button', { class: 'btn primary', type: 'button', onClick: confirm }, icon('close'), 'Remove from AMC');

  const showCredit = debounce(async () => {
    credit.style.display = basket.size ? '' : 'none';
    if (!basket.size) return;
    try {
      const r = await get('/api/amc-removal/impact', { keys: [...basket.keys()].join(','), date: when.value });
      const norate = r.assets.filter((a) => a.no_rate).length;
      credit.replaceChildren(icon('information--filled'), h('div', null, h('strong', null, `Credit due from the vendor: ${inr(r.total)}`),
        h('div', { class: 'small' }, `Quarterly rate × the days of ${r.quarter.label} not covered (${r.assets[0]?.days ?? 0} of ${r.quarter.days}). Parts are included.${norate ? ` ${norate} asset(s) have no rate recorded.` : ''}`)));
    } catch (e) { credit.replaceChildren(e.message); }
  }, 250);
  when.addEventListener('change', showCredit);

  function draw() {
    const rows = [...basket.values()];
    drawer.setTitle(`Basket · ${int(rows.length)}`, 'Remove from AMC');
    list.replaceChildren(rows.length ? h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Asset (CI)', 'Serial no.', 'Class / type', 'Rate / qtr', 'Parts', 'Reason', ''].map((t) => h('th', null, t)))),
      h('tbody', null, rows.map(({ row, reason }) => {
        const sel = h('select', { 'aria-label': `Reason for ${row.asset_key}` }, h('option', { value: '' }, 'Choose'), reasonOpts());
        sel.value = reason;
        sel.addEventListener('change', () => { basket.get(row.asset_key).reason = sel.value; });
        return h('tr', null, h('td', null, h('span', { class: 'mono' }, row.asset_key), h('div', { class: 'muted small' }, [row.make, row.model].filter(Boolean).join(' '))), h('td', { class: 'mono' }, row.serial_no || '—'),
          h('td', null, label(row.asset_class), h('div', { class: 'muted small' }, label(row.asset_type))), h('td', { class: 'num' }, inr(row.rate_value)),
          h('td', null, row.components ? `+${row.components}` : '', row.open_calls ? h('span', { class: 'badge warn', title: `${row.open_calls} open call(s)` }, icon('warning--alt--filled'), ` ${row.open_calls} call`) : null), h('td', null, sel),
          h('td', null, h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': `Take ${row.asset_key} out of the basket`, onClick: () => { basket.delete(row.asset_key); changed(); } }, icon('close'))));
      }))) : h('p', { class: 'muted' }, 'The basket is empty. Click rows in the register to add them, or use "Add all listed" or "Paste or upload a list".'));
    overrideRow.style.display = rows.some((x) => x.row.open_calls) ? '' : 'none';             // .frow sets its own display, which beats the hidden attribute
    submit.disabled = !rows.length;
    showCredit();
  }
  allReason.addEventListener('change', () => { if (allReason.value) { basket.forEach((x) => { x.reason = allReason.value; }); draw(); } });
  drawer.body.append(list, h('div', { class: 'stack-v', style: { marginTop: '12px' } }, frow(allReason.id, 'Same reason for every asset', allReason),
    frow(when.id, 'Effective date', when, `Any day of ${meta.quarter.label}; defaults to its first day. A later day than today waits and is applied automatically.`),
    frow(who.id, 'Verbal approval given by', who, 'Only an administrator or Team Leader/SI can give the approval.'), frow(note.id, 'Note', note), overrideRow, credit,
    h('div', { class: 'btn-row' }, submit)));
  const stop = (() => { subs.add(draw); return () => subs.delete(draw); })();
  const close = drawer.close;
  drawer.close = (...a) => { stop(); close(...a); };
  draw();

  function confirm() {
    const rows = [...basket.values()];
    if (!who.value) { toast('Choose who gave the verbal approval.', 'bad'); who.focus(); return; }
    if (rows.some((x) => !x.reason)) { toast('Give a reason for every asset.', 'bad'); return; }
    openModal({
      title: `Remove ${rows.length} asset${rows.length === 1 ? '' : 's'} from AMC?`,
      lead: `Effective ${when.value}. They are archived: they leave the Assets register, every count and the PM list. This can be undone with a reinstatement.`,
      body: h('div', null, credit.cloneNode(true), h('p', { class: 'muted small' }, rows.slice(0, 12).map((x) => x.row.asset_key).join(', ') + (rows.length > 12 ? ` and ${rows.length - 12} more` : ''))),
      actions: [{ label: 'Cancel' }, { label: 'Remove from AMC', danger: true, onClick: async () => {
        const r = await send('/api/amc-removal/remove', { keys: rows.map((x) => x.row.asset_key), reason: Object.fromEntries(rows.map((x) => [x.row.asset_key, x.reason])),
          effective_date: when.value, approved_by: who.value, note: note.value, override_note: override.value });
        toast(r.status === 'PENDING' ? `${r.assets.length} asset(s) will be removed on ${when.value} (${r.removal_no})` : `${r.assets.length} asset(s) removed from AMC (${r.removal_no})${r.components ? ` with ${r.components} parts` : ''}`);
        basket.clear(); changed(); drawer.close(true); onDone?.();
      } }],
    });
  }
}

function listDialog() {
  openModal({ title: 'Paste or upload a list', lead: 'Names, hostnames, serial numbers or ONGC IDs - or the filled-in upload template. Nothing is removed here: each line is checked and the ones that can go are added to the basket.',
    wide: true, body: listChecker({ onAdd: (rows) => addRows(rows), open: true }), actions: [{ label: 'Close' }] });
}

const downloadTemplate = async () => {
  try { await downloadGet('/api/amc-removal/template', {}, 'AMC_Removal_Upload_Template.xlsx'); toast('Template downloaded'); } catch (e) { toast(e.message, 'bad'); }
};

// ------------------------------------------------------------------ the page
/** A drop-down menu in the site's own style (the same markup as the Download / share menu). */
function dropMenu({ text, ico, items }) {
  const btn = h('button', { class: 'btn', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, icon(ico), text, icon('chevron--down', 'sm'));
  const menu = h('div', { class: 'menu', role: 'menu', hidden: true, style: { left: '0', right: 'auto', minWidth: '270px' } });
  const wrap = h('div', { class: 'usr-wrap' }, btn, menu);
  const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  items.forEach(([ico2, label2, fn]) => menu.append(h('button', { role: 'menuitem', type: 'button', onClick: () => { close(); fn(); } }, icon(ico2), label2)));
  btn.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; btn.setAttribute('aria-expanded', String(!menu.hidden)); if (!menu.hidden) menu.querySelector('button').focus(); });
  const away = (e) => { if (!menu.hidden && !wrap.contains(e.target)) close(); };
  document.addEventListener('click', away);
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') { close(); btn.focus(); } });
  wrap.dispose = () => document.removeEventListener('click', away);
  return wrap;
}

export function mountAmcPick(root) {
  let page = null;
  const basketBtn = h('button', { class: 'btn primary', type: 'button', title: 'Open the basket: reason, date, verbal approval and send', onClick: () => openBasket({ onDone: () => page?.refresh?.() }) });
  const reqLink = h('a', { class: 'btn', href: href('admin/amc-requests'), title: 'Every request, reinstating, and the Excel report' }, icon('catalog'), 'Requests');
  const inBasketEl = h('span', { class: 'count-line', role: 'status' });
  const paint = () => { basketBtn.replaceChildren(icon('package'), `Basket (${int(basket.size)}) — review and send`); inBasketEl.textContent = `${int(basket.size)} in the basket · click a row to add or remove it`; };
  const stopSub = (() => { subs.add(paint); return () => subs.delete(paint); })();
  paint();
  get('/api/amc-removal/summary').then((s) => { if (s.waiting) { reqLink.replaceChildren(icon('catalog'), `Requests (${s.waiting} waiting)`); reqLink.classList.add('primary'); } }).catch(() => {});

  const step = (n, text) => h('span', null, h('b', null, `${n} `), text);
  const steps = h('div', { class: 'pick-steps', 'aria-label': 'How it works' }, step(1, 'Filter on the left, or search'), step(2, 'Click rows, or add everything listed'), step(3, 'Open the basket: reason, date, approver, send'));

  let timer = null, menus = [];
  const pick = {
    title: 'Remove from AMC',
    sub: 'Removed assets are archived, never deleted. Reinstating is possible from the requests page.',
    steps,
    isPicked: inBasket,
    toggle,
    ready: (h2) => { page = h2; subs.add(h2.restyle); },
    head: () => [reqLink, basketBtn],
    // The buttons that act on the list sit on the list, beside the count they refer to.
    bar: (api) => {
      const addAll = h('button', { class: 'btn', type: 'button', title: 'Add every asset matching the current search and filters (those that can be removed) to the basket', onClick: async () => {
        try { await addKeys(await api.allKeys(2000)); } catch (e) { toast(e.message, 'bad'); }
      } });
      const label2 = () => { addAll.replaceChildren(icon('add'), api.total ? `Add all listed (${int(api.total)})` : 'Add all listed'); };
      label2(); timer = setInterval(label2, 700);                       // the count follows the filters
      const fromList = dropMenu({ text: 'From a list', ico: 'upload', items: [['upload', 'Paste or upload a list…', listDialog], ['download', 'Download upload template', downloadTemplate]] });
      menus.push(fromList);
      return h('div', { class: 'pick-bar' }, h('strong', null, 'Add to basket'), addAll, fromList, h('span', { style: { marginLeft: 'auto' } }, inBasketEl));
    },
  };
  const reg = mountRegister(root, 'assets', { pick });
  const done = reg.destroy;
  reg.destroy = () => { stopSub(); clearInterval(timer); menus.forEach((m) => m.dispose?.()); if (page) subs.delete(page.restyle); done?.(); };          // the register's own object stays (it has live getters)
  return reg;
}
