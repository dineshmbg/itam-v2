// Remove from AMC - pick many assets of any class and type into one basket, then send them as ONE request.
// Left: find assets (only those that can be removed are listed). Right: the basket and the request form. The basket survives changing the
// filters, so a switch, a UPS and ten desktops can go in together. The server checks everything again - see portal/app/amcremoval.py.
import { get, send } from '../core/api.js';
import { debounce, h, icon } from '../core/dom.js';
import { int, titleCase } from '../core/format.js';
import { toast } from '../ui/toast.js';
import { openModal } from '../ui/modal.js';
import { errorBlock, pageHead, panel } from './common.js';
import { chip, listChecker } from './amc-list.js';

const inr = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const label = (s) => titleCase(String(s || '').replace(/_/g, ' '));
let seq = 0;

export function mountAmcPicker(root) {
  let dead = false, meta = null, facets = null, last = null;
  const basket = new Map();                                   // asset_key -> { row, reason }
  const f = { q: '', cls: '', type: '', engineer: '', location: '', components: false };
  const body = h('div', { class: 'grid' });
  root.append(h('div', { class: 'page' }, pageHead('Remove from AMC', 'Choose assets of any class and type, check the credit, and remove them from the AMC in one request. Nothing is deleted: removed assets are archived.'), body));

  const field = (text, control, hint) => {
    const id = 'amc' + (++seq);
    control.id = id;
    return h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, text), control, hint ? h('div', { class: 'muted small' }, hint) : null);
  };
  const select = (opts, onChange) => { const s = h('select', null, opts.map(([v, t]) => h('option', { value: v }, t))); s.addEventListener('change', () => onChange(s.value)); return s; };

  // ------------------------------------------------------------------ find
  const results = h('div', { class: 'tbl-wrap', style: { maxHeight: '60vh', overflow: 'auto' } });       // 300 rows must not stretch the page (or the basket beside it)
  const summary = h('div', { class: 'muted small', role: 'status' });
  const search = h('input', { type: 'search', placeholder: 'Asset key, hostname, serial, make, model, ONGC ID or user', autocomplete: 'off' });
  const selClass = select([['', 'All classes']], (v) => { f.cls = v; f.type = ''; drawTypes(); run(); });
  const selType = select([['', 'All types']], (v) => { f.type = v; run(); });
  const selEng = select([['', 'Any engineer']], (v) => { f.engineer = v; run(); });
  const selLoc = select([['', 'Any location']], (v) => { f.location = v; run(); });
  const comp = h('input', { type: 'checkbox' });
  comp.addEventListener('change', () => { f.components = comp.checked; run(); });
  search.addEventListener('input', debounce(() => { f.q = search.value.trim(); run(); }, 250));

  const fill = (el, first, list) => el.replaceChildren(h('option', { value: '' }, first), ...list.map((x) => h('option', { value: x.value }, `${label(x.value)} (${int(x.n)})`)));
  function drawTypes() { fill(selType, 'All types', (facets.types || []).filter((t) => !f.cls || t.asset_class === f.cls)); selType.value = f.type; }
  function drawFacets() {
    fill(selClass, 'All classes', facets.classes); selClass.value = f.cls;
    drawTypes();
    fill(selEng, 'Any engineer', facets.engineers); selEng.value = f.engineer;
    fill(selLoc, 'Any location', facets.locations); selLoc.value = f.location;
  }

  const params = () => ({ q: f.q, classes: f.cls, types: f.type, engineer: f.engineer, location: f.location, components: f.components ? '1' : '0' });

  async function run() {
    try { last = await get('/api/amc-removal/candidates', params()); } catch (e) { results.replaceChildren(errorBlock(e, run)); return; }
    if (dead) return;
    facets = last.facets;
    if (!selClass.options.length || selClass.options.length === 1) drawFacets();
    drawResults();
  }

  const addRows = (rows) => { rows.forEach((r) => { if (!basket.has(r.asset_key)) basket.set(r.asset_key, { row: r, reason: defaultReason.value }); }); drawBasket(); drawResults(); };

  function drawResults() {
    const rows = last.rows;
    const shown = rows.filter((r) => !basket.has(r.asset_key));
    summary.textContent = `${int(rows.length)} listed${last.capped ? ` of more than ${int(rows.length)} - narrow the search` : ''}${last.total != null ? ` · ${int(last.total)} match` : ''}. Laptops, warranty-only and already-removed assets are never listed.`;
    const tools = h('div', { class: 'btn-row', style: { margin: '8px 0' } },
      h('button', { class: 'btn', type: 'button', disabled: !shown.length, onClick: () => addRows(shown) }, icon('add'), `Add the ${int(shown.length)} shown`),
      last.keys.length > rows.length ? h('button', { class: 'btn', type: 'button', onClick: async () => {
        try { addRows((await get('/api/amc-removal/candidates', { ...params(), all: '1' })).rows); } catch (e) { toast(e.message, 'bad'); }
      } }, icon('add'), `Add all ${int(last.keys.length)} matching`) : null);
    const tbl = h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['', 'Asset (CI)', 'Class / type', 'Engineer', 'Rate / qtr', 'Parts'].map((t) => h('th', null, t)))),
      h('tbody', null, rows.length ? rows.map((r) => {
        const inB = basket.has(r.asset_key);
        const cb = h('input', { type: 'checkbox', 'aria-label': `Add ${r.asset_key} to the basket` });
        cb.checked = inB;
        cb.addEventListener('change', () => { if (cb.checked) addRows([r]); else { basket.delete(r.asset_key); drawBasket(); drawResults(); } });
        return h('tr', { class: inB ? 'sel' : null }, h('td', null, cb),
          h('td', null, h('span', { class: 'mono' }, r.asset_key, r.record_level === 'COMPONENT' ? h('span', { class: 'muted small' }, ' (part)') : null), h('div', { class: 'muted small' }, [r.make, r.model].filter(Boolean).join(' '))),
          h('td', null, label(r.asset_class), h('div', { class: 'muted small' }, label(r.asset_type))), h('td', null, r.engineer_name || '—'),
          h('td', { class: 'num' }, inr(r.rate_value)), h('td', null, r.components ? `+${r.components}` : '', r.open_calls ? h('span', { class: 'badge warn', title: `${r.open_calls} open call(s)` }, ` ${r.open_calls} call`) : null));
      }) : [h('tr', null, h('td', { colspan: '6', class: 'muted' }, 'No eligible asset matches.'))]));
    results.replaceChildren(tbl);
    findTools.replaceChildren(tools);
  }
  const findTools = h('div');

  // ------------------------------------------------------------------ basket + request
  const defaultReason = h('select', null);
  const when = h('input', { type: 'date' });
  const who = h('select', null);
  const note = h('input', { type: 'text', maxlength: '300', placeholder: 'Optional' });
  const override = h('input', { type: 'text', maxlength: '300', placeholder: 'Why is it still right to remove assets with open calls?' });
  const list = h('div', { class: 'stack-v' });
  const credit = h('div', { class: 'rec-note info', role: 'status' });
  const submit = h('button', { class: 'btn primary', type: 'button', onClick: confirm }, icon('close'), 'Remove from AMC');
  const overrideRow = field('Override note (required)', override);
  const reasonOpts = () => meta.reasons.map((r) => h('option', { value: r.value }, r.label));

  const showCredit = debounce(async () => {
    credit.style.display = basket.size ? '' : 'none';
    if (!basket.size) { credit.replaceChildren(); return; }
    try {
      const r = await get('/api/amc-removal/impact', { keys: [...basket.keys()].join(','), date: when.value });
      const norate = r.assets.filter((a) => a.no_rate).length;
      credit.replaceChildren(icon('information--filled'), h('div', null, h('strong', null, `Credit due from the vendor: ${inr(r.total)}`),
        h('div', { class: 'small' }, `Quarterly rate × the days of ${r.quarter.label} not covered (${r.assets[0]?.days ?? 0} of ${r.quarter.days}). Parts are included.${norate ? ` ${norate} asset(s) have no rate recorded.` : ''}`)));
    } catch (e) { credit.replaceChildren(e.message); }
  }, 250);
  when.addEventListener('change', showCredit);

  function drawBasket() {
    const rows = [...basket.values()];
    list.replaceChildren(...(rows.length ? rows.map(({ row, reason }) => {
      const sel = h('select', { 'aria-label': `Reason for ${row.asset_key}` }, reasonOpts());
      sel.value = reason;
      sel.addEventListener('change', () => { basket.get(row.asset_key).reason = sel.value; });
      return h('div', { class: 'frow', style: { display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: '8px', alignItems: 'center' } },
        h('span', null, h('strong', { class: 'mono' }, row.asset_key), h('div', { class: 'muted small' }, `${label(row.asset_class)} ${label(row.asset_type)}${row.components ? ` · +${row.components} parts` : ''}`)), sel,
        h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': `Take ${row.asset_key} out of the basket`, onClick: () => { basket.delete(row.asset_key); drawBasket(); drawResults(); } }, icon('close')));
    }) : [h('p', { class: 'muted' }, 'The basket is empty. Tick assets on the left; the basket stays while you change the filters.')]));
    const open = rows.some((x) => x.row.open_calls);
    overrideRow.style.display = open ? '' : 'none';           // .frow sets its own display, which beats the hidden attribute
    submit.disabled = !rows.length;
    countEl.textContent = `${int(rows.length)} in the basket`;
    showCredit();
  }
  const countEl = h('span', { class: 'hint' });

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
        basket.clear(); drawBasket(); run();
      } }],
    });
  }

  // ------------------------------------------------------------------ start
  (async () => {
    try {
      meta = await get('/api/amc-removal/meta');
      if (dead) return;
      defaultReason.replaceChildren(h('option', { value: '' }, 'Choose a reason'), ...reasonOpts());
      defaultReason.addEventListener('change', () => { if (defaultReason.value) { basket.forEach((x) => { x.reason = defaultReason.value; }); drawBasket(); } });
      who.replaceChildren(h('option', { value: '' }, 'Choose who approved'), ...meta.approvers.map((a) => h('option', { value: a.username }, `${a.display_name} (${a.username})`)));
      when.value = meta.default_date; when.min = meta.quarter.start; when.max = meta.quarter.end;
      body.replaceChildren(
        panel('1 · Find assets', { cls: 'span-7', hint: 'only assets that can be removed are listed' }, h('div', null,
          field('Search', search), h('div', { class: 'btn-row' }, selClass, selType, selEng, selLoc),
          h('label', { class: 'ropt', style: { marginTop: '8px' } }, comp, icon('checkbox', 'glyph off'), icon('checkbox--checked', 'glyph on'), h('span', null, 'Also list parts (monitor, graphics card ...) on their own - otherwise they leave with their asset')),
          listChecker({ onAdd: (rows) => addRows(rows) }), findTools, summary, results)),
        panel('2 · Basket and request', { cls: 'span-5', hint: '' }, h('div', null, countEl,
          field('Reason for new additions (and all, when changed)', defaultReason), list,
          field('Effective date', when, `Any day of ${meta.quarter.label}; defaults to its first day. A later day than today waits and is applied automatically.`),
          field('Verbal approval given by', who), field('Note', note), overrideRow, credit,
          h('div', { class: 'btn-row', style: { marginTop: '12px' } }, submit))));
      Object.assign(body.lastElementChild.style, { alignSelf: 'start', position: 'sticky', top: '12px' });      // the basket stays in view while the list scrolls
      // ---- requests from engineers that wait for a decision
      const waitBox = h('div', { class: 'stack-v' });
      const waiting = panel('Waiting for a decision', { cls: 'span-12', hint: 'requests from engineers for their own assets' }, waitBox);
      waiting.style.display = 'none';
      body.prepend(waiting);
      const reasonName = (v) => meta.reasons.find((x) => x.value === v)?.label || v;
      const card = (r) => {
        const who2 = h('select', { 'aria-label': 'Verbal approval given by' }, h('option', { value: '' }, 'Choose who approved'), ...meta.approvers.map((a) => h('option', { value: a.username }, `${a.display_name} (${a.username})`)));
        const date2 = h('input', { type: 'date', min: meta.quarter.start, max: meta.quarter.end, value: r.effective_date < meta.quarter.start ? meta.quarter.start : r.effective_date, 'aria-label': 'Effective date' });
        const ovr = h('input', { type: 'text', maxlength: '300', placeholder: 'Override note (only if it still has open calls)', 'aria-label': 'Override note' });
        const why = h('input', { type: 'text', maxlength: '300', placeholder: 'Reason for turning it down (the engineer reads this)', 'aria-label': 'Reason for turning it down' });
        const done = async (fn, msg) => { try { await fn(); toast(msg); loadWaiting(); run(); } catch (e) { toast(e.message, 'bad'); } };
        return h('div', { class: 'rec-note warn', style: { display: 'block' } },
          h('div', null, chip('warn', 'pending', r.removal_no), ` from ${r.requested_by} · ${r.lines.filter((x) => !x.via_parent).length} asset(s)${r.note ? ` · "${r.note}"` : ''}`),
          h('div', { class: 'muted small' }, r.lines.filter((x) => !x.via_parent).map((x) => `${x.asset_key} (${reasonName(x.reason)})`).join(', ')),
          h('div', { class: 'btn-row', style: { margin: '8px 0' } }, who2, date2, ovr,
            h('button', { class: 'btn primary', type: 'button', onClick: () => done(() => send('/api/amc-removal/approve', { removal_no: r.removal_no, approved_by: who2.value, effective_date: date2.value, override_note: ovr.value }), `${r.removal_no} approved`) }, icon('checkmark'), 'Approve')),
          h('div', { class: 'btn-row' }, why, h('button', { class: 'btn', type: 'button', onClick: () => done(() => send('/api/amc-removal/reject', { removal_no: r.removal_no, reason: why.value }), `${r.removal_no} turned down`) }, icon('close'), 'Turn down')));
      };
      async function loadWaiting() {
        try { const rows = (await get('/api/amc-removal/requests', { status: 'REQUESTED' })).rows; waiting.style.display = rows.length ? '' : 'none'; waitBox.replaceChildren(...rows.map(card)); } catch (e) { /* the page still works without it */ }
      }
      loadWaiting();
      drawBasket();
      await run();
    } catch (e) { body.replaceChildren(errorBlock(e)); }
  })();

  return { destroy() { dead = true; } };
}
