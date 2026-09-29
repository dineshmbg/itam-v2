// Fill IMAC: a portal-native record of Install/Add/Change work against an asset, started from its detail page.
// Almost everything on the form is automatic - the current date, the asset's own identity fields, and the
// signed-in engineer's own name. The one interactive part is the requester: pulled from the asset's registered
// user when it has one, or found by searching a CPF number or name when it does not (see portal/app/imac.py).
import { downloadGet, get, send } from '../core/api.js';
import { debounce, h, icon } from '../core/dom.js';
import { toast } from '../core/editor.js';
import { openModal } from '../ui/modal.js';

function field(label, value) {
  return h('div', { class: 'frow' }, h('label', { class: 'flabel' }, label), h('input', { type: 'text', value: value || '', disabled: true }));
}

/** Type a name or CPF number; suggestions come from the portal's global search (dataset "employees"). Picking a
 *  suggestion resolves the person's name and mobile through /imac/person - the CPF the browser shows is not
 *  trusted for the mobile number, that is always re-read from the employee master on save too. */
function personSearch(assetKey, onPick) {
  const list = h('datalist', { id: 'imac-person-l' });
  const i = h('input', { type: 'text', maxlength: '40', list: 'imac-person-l', class: 'mono upper', placeholder: 'Type a name or CPF number' });
  const look = debounce(async () => {
    const q = i.value.trim();
    if (q.length < 2) return;
    try {
      const r = await get('/api/search', { q });
      const g = r.groups.find((x) => x.dataset === 'employees');
      list.replaceChildren(...(g ? g.items.map((it) => h('option', { value: it.id }, it.sub ? `${it.title} - ${it.sub}` : it.title)) : []));
    } catch (_) { /* suggestions are optional */ }
  }, 180);
  i.addEventListener('input', look);
  i.addEventListener('blur', async () => {
    i.value = i.value.trim().toUpperCase();
    if (!i.value) return;
    try {
      const r = await get(`/api/assets/${encodeURIComponent(assetKey)}/imac/person`, { cpf: i.value });
      if (!r.person) toast('No matching person found - pick a suggestion, or check the CPF number.', 'bad');
      onPick(r.person);
    } catch (e) { toast(e.message, 'bad'); }
  });
  return h('div', { class: 'linked' }, i, list);
}

/** `assetKey`: the asset whose detail page the button was pressed on. `row`: that asset's already-loaded detail
 *  row, used only for its identity fields shown while /imac/context loads. */
export function imacDialog({ assetKey, row, onDone }) {
  let requester = null;

  const reqBox = h('div', null, h('span', { class: 'muted small' }, 'Loading…'));
  function renderRequester(person) {
    requester = person;
    reqBox.replaceChildren(person
      ? h('div', { class: 'frow3' }, field('CPF no.', person.cpf), field('User name', person.name), field('Mobile no.', person.mobile))
      : h('div', null, h('div', { class: 'rec-note warn' }, icon('warning--alt--filled'), 'No user is on file for this asset - search for the requester by CPF number or name.'),
          h('div', { class: 'frow' }, h('label', { class: 'flabel' }, 'Requester (name or CPF no.)'), personSearch(assetKey, (p) => renderRequester(p)))));
  }

  const dateBox = h('div', { class: 'frow4' },
    field('Asset type', row.asset_type), field('Model', [row.make, row.model].filter(Boolean).join(' ')),
    field('Asset ID', assetKey), field('Serial no.', row.serial_no));
  const engBox = h('div', { class: 'frow' }, h('label', { class: 'flabel' }, 'Engineer'), h('input', { type: 'text', value: '', disabled: true, id: 'imac-eng' }));
  const dateField = h('div', { class: 'frow' }, h('label', { class: 'flabel' }, 'Date'), h('input', { type: 'text', value: '', disabled: true, id: 'imac-date' }));

  const body = h('div', { class: 'imac-form' },
    h('div', { class: 'rec-note info' }, icon('information--filled'), 'Date, asset details and engineer are filled in automatically.'),
    dateField,
    h('h3', null, 'Requester'), reqBox,
    h('h3', null, 'Asset details'), dateBox,
    h('h3', null, 'Engineer'), engBox);

  const modal = openModal({
    title: `Fill IMAC - ${assetKey}`, wide: true, body,
    actions: [{ label: 'Cancel' }, { label: 'Generate PDF', primary: true, icon: 'save', keepOpen: true, onClick: async () => {
      const values = { key: assetKey, requester_cpf: requester?.cpf || '' };
      const r = await send('/api/edit/assets/imac', values);
      toast('IMAC form saved');
      onDone?.(r.detail);
      try { await downloadGet(`/api/imac/${r.id}/pdf`, null, `IMAC_${assetKey}.pdf`); } catch (e) { toast('Saved, but the PDF could not be downloaded: ' + e.message, 'bad'); }
    } }],
  });

  (async () => {
    try {
      const ctx = await get(`/api/assets/${encodeURIComponent(assetKey)}/imac/context`);
      modal.el.querySelector('#imac-date').value = new Date(ctx.date).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
      modal.el.querySelector('#imac-eng').value = ctx.engineer_name;
      renderRequester(ctx.requester);
    } catch (e) { modal.error(e.message); }
  })();
}
