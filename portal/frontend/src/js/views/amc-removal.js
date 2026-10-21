// Remove an asset from AMC (administrators and Team Leader/SI). The server checks everything again - see portal/app/amcremoval.py.
import { get, send } from '../core/api.js';
import { h } from '../core/dom.js';
import { user as sessionUser } from '../core/session.js';
import { toast } from '../ui/toast.js';
import { openModal } from '../ui/modal.js';

const frow = (id, label, control, hint) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), control, hint ? h('div', { class: 'muted small' }, hint) : null);

/** onDone(removalNo) runs after the asset has been removed (it is archived, so the caller closes its page and refreshes). */
export async function amcRemovalDialog({ asset, onDone }) {
  const key = asset.asset_key;
  let pre;
  try { pre = await get('/api/amc-removal/preflight', { key }); } catch (e) { toast(e.message, 'bad'); return; }
  if (pre.blocked) { toast(`${key} cannot be removed from AMC: ${pre.blocked}.`, 'bad'); return; }
  const reason = h('select', { id: 'amc-reason' }, h('option', { value: '' }, 'Choose a reason'), pre.reasons.map((r) => h('option', { value: r.value }, r.label)));
  const when = h('input', { id: 'amc-date', type: 'date', value: pre.default_date, min: pre.quarter.start, max: pre.quarter.end });
  const who = h('select', { id: 'amc-who' }, h('option', { value: '' }, 'Choose who approved'), pre.approvers.map((a) => h('option', { value: a.username }, `${a.display_name} (${a.username})`)));
  const me = sessionUser()?.username;
  if (me && pre.approvers.some((a) => a.username === me)) who.value = me;           // the person at the keyboard is usually the one who approved
  const note = h('input', { id: 'amc-note', type: 'text', maxlength: '300', placeholder: 'Optional' });
  const credit = h('p', { class: 'rec-note info', role: 'status' });
  const showCredit = async () => {
    try {
      const i = (await get('/api/amc-removal/impact', { keys: key, date: when.value })).assets[0];
      credit.textContent = i.no_rate ? 'No rate is recorded for this asset, so no credit can be worked out.'
        : `Credit due from the vendor: ${i.amount} (rate ${pre.asset.rate_value} per quarter x ${i.days} of the quarter's days not covered).`;
    } catch (e) { credit.textContent = e.message; }
  };
  when.addEventListener('change', showCredit);
  showCredit();
  const open = pre.open_calls + pre.open_rma;
  const override = h('input', { id: 'amc-override', type: 'text', maxlength: '300', placeholder: 'Why is it still right to remove it?' });
  openModal({
    title: `Remove ${key} from AMC`,
    lead: `The asset is archived: it leaves the Assets register, every count and the PM list, and nothing is deleted. Running quarter: ${pre.quarter.label}.`,
    body: h('div', null,
      frow('amc-reason', 'Reason', reason), frow('amc-date', 'Effective date', when, `Any day of ${pre.quarter.label} (${pre.quarter.start} to ${pre.quarter.end}); defaults to its first day. A later day than today waits and is applied automatically.`),
      frow('amc-who', 'Verbal approval given by', who), frow('amc-note', 'Note', note),
      open ? frow('amc-override', 'Override note (required)', override, `Still has ${pre.open_calls} open call(s) and ${pre.open_rma} OEM RMA case(s) not yet returned.`) : null,
      pre.components ? h('p', { class: 'rec-note info' }, `Its ${pre.components} part(s) (monitor, graphics card ...) leave with it and their rates are credited too.`) : null,
      credit, h('p', { class: 'muted small' }, 'The rate is saved on the request and cleared from the asset.')),
    actions: [{ label: 'Cancel' }, { label: 'Remove from AMC', danger: true, onClick: async () => {
      const r = await send('/api/amc-removal/remove', { keys: [key], reason: reason.value, effective_date: when.value, approved_by: who.value, note: note.value, override_note: override.value });
      toast(r.status === 'PENDING' ? `${key} will be removed on ${when.value} (${r.removal_no})` : `${key} removed from AMC (${r.removal_no})`);
      onDone?.(r.removal_no);
    } }],
  });
}

/** Put assets that were removed from AMC back. keys: the removed assets (their parts return with them). onDone() runs after they are back. */
export async function amcReinstateDialog({ keys, onDone }) {
  let meta;
  try { meta = await get('/api/amc-removal/meta'); } catch (e) { toast(e.message, 'bad'); return; }
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const who = h('select', { id: 'ri-who' }, h('option', { value: '' }, 'Choose who approved'), meta.approvers.map((a) => h('option', { value: a.username }, `${a.display_name} (${a.username})`)));
  const when = h('input', { id: 'ri-date', type: 'date', value: today, min: meta.quarter.start, max: today });
  const me = sessionUser()?.username;
  if (me && meta.approvers.some((a) => a.username === me)) who.value = me;
  const why = h('input', { id: 'ri-why', type: 'text', maxlength: '300', placeholder: 'Why is it going back into the AMC?' });
  openModal({
    title: `Reinstate ${keys.length === 1 ? keys[0] : keys.length + ' assets'} in AMC`,
    lead: 'The asset leaves the archive and gets back the cover, rate and PM state it had. Its parts return with it. The removal request stays as the record.',
    body: h('div', null, frow('ri-why', 'Reason (required)', why), frow('ri-date', 'Billed again from', when, 'The credit kept is only for the days it was out.'), frow('ri-who', 'Verbal approval given by', who)),
    actions: [{ label: 'Cancel' }, { label: 'Reinstate', primary: true, onClick: async () => {
      const r = await send('/api/amc-removal/reinstate', { keys, reason: why.value, effective_date: when.value, approved_by: who.value });
      toast(`${r.reinstated.length} asset(s) back in the AMC`);
      onDone?.(r);
    } }],
  });
}
