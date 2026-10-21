// "AMC removals" for an engineer: ask for some of YOUR OWN assets to be taken out of the AMC, follow what became of the request, and see - read only -
// which of your assets have already left the AMC. Those are archived, so they are no longer part of your asset list or counts.
// A team leader or administrator decides the request (Data tools > Remove from AMC).
import { get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { date, int, titleCase } from '../core/format.js';
import { toast } from '../ui/toast.js';
import { errorBlock, pageHead, panel } from './common.js';
import { listChecker, statusChip } from './amc-list.js';

const label = (s) => titleCase(String(s || '').replace(/_/g, ' '));

export function mountAmcMine(root) {
  let dead = false, data = null;
  const body = h('div', { class: 'grid' });
  root.append(h('div', { class: 'page' }, pageHead('Remove from AMC', 'Ask for assets of yours to be taken out of the AMC. A team leader decides; nothing changes until then.'), body));
  const picked = new Map();                                    // asset_key -> row

  async function load() {
    try { data = await get('/api/amc-removal/mine'); } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, load)); return; }
    if (!dead) draw();
  }

  function draw() {
    const reason = h('select', { id: 'am-reason', 'aria-label': 'Reason' }, h('option', { value: '' }, 'Choose a reason'), data.reasons.map((r) => h('option', { value: r.value }, r.label)));
    const note = h('input', { id: 'am-note', type: 'text', maxlength: '300', placeholder: 'Anything the team leader should know (optional)' });
    const count = h('span', { class: 'muted small', role: 'status' });
    const tick = () => { count.textContent = `${int(picked.size)} selected`; send_.disabled = !picked.size; };
    const send_ = h('button', { class: 'btn primary', type: 'button', onClick: async () => {
      if (!reason.value) { toast('Choose a reason.', 'bad'); return; }
      try {
        const r = await send('/api/amc-removal/request', { keys: [...picked.keys()], reason: reason.value, note: note.value });
        toast(`Request ${r.removal_no} sent for a decision`);
        picked.clear(); await load();
      } catch (e) { toast(e.message, 'bad'); }
    } }, icon('send'), 'Send request for approval');
    const rows = data.assets.map((a) => {
      const cb = h('input', { type: 'checkbox', 'aria-label': `Select ${a.asset_key}` });
      cb.checked = picked.has(a.asset_key);
      cb.addEventListener('change', () => { if (cb.checked) picked.set(a.asset_key, a); else picked.delete(a.asset_key); tick(); });
      return { a, cb };
    });
    const mineTbl = rows.length ? h('div', { class: 'tbl-wrap', style: { maxHeight: '45vh', overflow: 'auto' } }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['', 'Asset (CI)', 'Class / type', 'Location'].map((t) => h('th', null, t)))),
      h('tbody', null, rows.map(({ a, cb }) => h('tr', null, h('td', null, cb), h('td', null, h('span', { class: 'mono' }, a.asset_key), h('div', { class: 'muted small' }, [a.make, a.model].filter(Boolean).join(' '))),
        h('td', null, label(a.asset_class), h('div', { class: 'muted small' }, label(a.asset_type))), h('td', null, a.location_code || '—')))))) : h('p', { class: 'muted' }, 'You have no assets that can be removed from the AMC.');
    const checker = listChecker({ onAdd: (found) => { found.forEach((r) => picked.set(r.asset_key, r)); rows.forEach(({ a, cb }) => { cb.checked = picked.has(a.asset_key); }); tick(); } });
    tick();
    const reqs = data.requests.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Request', 'Assets', 'Status', 'Decision', ''].map((t) => h('th', null, t)))),
      h('tbody', null, data.requests.map((r) => h('tr', null, h('td', { class: 'mono' }, r.removal_no, h('div', { class: 'muted small' }, date(r.created_at))),
        h('td', null, r.lines.filter((x) => !x.via_parent).map((x) => x.asset_key).join(', ')), h('td', null, statusChip(r.status)),
        h('td', null, r.status === 'REJECTED' ? r.decision_note : r.status === 'APPLIED' ? `From ${date(r.effective_date)}` : r.status === 'PENDING' ? `On ${date(r.effective_date)}` : ''),
        h('td', null, r.status === 'REQUESTED' ? h('button', { class: 'btn', type: 'button', onClick: async () => { try { await send('/api/amc-removal/withdraw', { removal_no: r.removal_no }); toast('Request withdrawn'); load(); } catch (e) { toast(e.message, 'bad'); } } }, 'Withdraw') : null)))))) : h('p', { class: 'muted' }, 'You have not made any request.');
    const gone = data.removed.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Asset (CI)', 'Class / type', 'Removed from AMC', 'Request'].map((t) => h('th', null, t)))),
      h('tbody', null, data.removed.map((r) => h('tr', null, h('td', { class: 'mono' }, r.asset_key, r.via_parent ? h('span', { class: 'muted small' }, ` (part of ${r.via_parent})`) : null),
        h('td', null, `${label(r.asset_class)} / ${label(r.asset_type)}`), h('td', null, date(r.effective_date)), h('td', { class: 'mono' }, r.removal_no)))))) : h('p', { class: 'muted' }, 'None of your assets has been removed from the AMC.');
    body.replaceChildren(
      panel('1 · Ask for removal', { cls: 'span-7', hint: 'your own assets only' }, h('div', null, checker, mineTbl,
        h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'am-reason' }, 'Reason'), reason), h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'am-note' }, 'Note'), note),
        h('div', { class: 'btn-row' }, send_, count))),
      panel('2 · My requests', { cls: 'span-5' }, reqs),
      panel('3 · Already removed from the AMC', { cls: 'span-12', hint: 'read only - these are archived, so they are not counted in your assets' }, gone));
  }
  load();
  return { destroy() { dead = true; } };
}
