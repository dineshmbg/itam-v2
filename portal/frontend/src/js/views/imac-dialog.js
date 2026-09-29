// Fill IMAC: a portal-native record of Install/Add/Change work against an asset, started from its detail page.
// The asset's own identity fields are pulled in automatically; a Change record may name a different Asset (CI) -
// the one actually left in place - which is how "old CI -> new CI" gets recorded (see portal/app/imac.py).
import { downloadGet, get, send } from '../core/api.js';
import { debounce, h, icon } from '../core/dom.js';
import { toast } from '../core/editor.js';
import { isAdmin } from '../core/session.js';
import { openModal } from '../ui/modal.js';

const todayISO = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };

function field(label, input) {
  return h('div', { class: 'frow' }, h('label', { class: 'flabel' }, label), input);
}

function locked(value) {
  return h('input', { type: 'text', class: 'mono', value: value || '', disabled: true });
}

/** Native radios styled the same way the rest of the app renders a short exclusive choice (see record.js's verifyDialog). */
function radioGroup(name, options, initial) {
  const inputs = [];
  const wrap = h('div', { class: 'rgroup', role: 'radiogroup' }, options.map(([v, t]) => {
    const i = h('input', { type: 'radio', name, value: v });
    i.checked = v === initial;
    inputs.push(i);
    return h('label', { class: 'ropt' }, i, icon('radio-button', 'glyph off'), icon('radio-button--checked', 'glyph on'), h('span', null, t));
  }));
  return { node: wrap, value: () => inputs.find((i) => i.checked)?.value || '' };
}

/** Type a name or CPF number; suggestions come from the same global search the rest of the portal uses for people
 *  (dataset "employees"). Picking a suggestion fills in the CPF number - what is actually stored - same convention
 *  as the "cpf"-kind field in ui/form.js. The matching name is kept alongside (from the same search results, or the
 *  asset's own registered user for the starting value) since the CPF alone is not enough to print on the form. */
function personField(id, initialCpf, initialLabel) {
  const names = new Map(initialCpf && initialLabel ? [[initialCpf, initialLabel]] : []);
  const list = h('datalist', { id });
  const i = h('input', { type: 'text', maxlength: '40', list: id, class: 'mono upper', placeholder: 'Type a name or CPF number' });
  i.value = initialCpf || '';
  if (initialCpf && initialLabel) list.append(h('option', { value: initialCpf }, initialLabel));
  const look = debounce(async () => {
    const q = i.value.trim();
    if (q.length < 2) return;
    try {
      const r = await get('/api/search', { q });
      const g = r.groups.find((x) => x.dataset === 'employees');
      const items = g ? g.items : [];
      items.forEach((it) => names.set(it.id, it.title));
      list.replaceChildren(...items.map((it) => h('option', { value: it.id }, it.sub ? `${it.title} - ${it.sub}` : it.title)));
    } catch (_) { /* suggestions are optional */ }
  }, 180);
  i.addEventListener('input', look);
  i.addEventListener('blur', () => { i.value = i.value.trim().toUpperCase(); });
  return { node: h('div', { class: 'linked' }, i, list), value: () => i.value.trim(), name: () => names.get(i.value.trim()) || '' };
}

/** Type an Asset (CI); suggestions from the same global search (dataset "assets"). Used only for a Change record that
 *  actually swaps the asset in place - selecting one re-reads its own type/make/model/serial so the snapshot on the
 *  saved record (and the printed PDF) describes the asset that was really left behind, not the one the button was
 *  pressed on. */
function assetField(id, onPick) {
  const list = h('datalist', { id });
  const i = h('input', { type: 'text', maxlength: '40', list: id, class: 'mono upper', placeholder: 'CI number of the asset left in place' });
  const look = debounce(async () => {
    const q = i.value.trim();
    if (q.length < 2) return;
    try {
      const r = await get('/api/search', { q });
      const g = r.groups.find((x) => x.dataset === 'assets');
      list.replaceChildren(...(g ? g.items.map((it) => h('option', { value: it.id }, it.sub)) : []));
    } catch (_) { /* suggestions are optional */ }
  }, 180);
  i.addEventListener('input', look);
  i.addEventListener('blur', async () => {
    i.value = i.value.trim().toUpperCase();
    if (i.value) await onPick(i.value);
  });
  return { node: h('div', { class: 'linked' }, i, list), value: () => i.value.trim() };
}

/** `assetKey`: the asset whose detail page the button was pressed on. `row`: that asset's already-loaded detail row -
 *  everything needed to prefill comes from what is already on screen, no extra round trip for the common case. */
export function imacDialog({ assetKey, row, onDone }) {
  let snapshot = { asset_type: row.asset_type, make: row.make, model: row.model, serial_no: row.serial_no, asset_key: assetKey };
  const snapCells = { type: locked(snapshot.asset_type), model: locked([snapshot.make, snapshot.model].filter(Boolean).join(' ')), key: locked(snapshot.asset_key), serial: locked(snapshot.serial_no) };
  const changeNote = h('div', { class: 'rec-note info', hidden: true });

  const changeType = radioGroup('imac-type', [['INSTALLATION', 'Installation'], ['ADDITION', 'Addition'], ['CHANGE', 'Change']], 'INSTALLATION');
  const ticket = h('input', { type: 'text', maxlength: '60', placeholder: 'e.g. SR-2026-01452' });
  const date = h('input', { type: 'date', value: todayISO() });
  const section = h('input', { type: 'text', maxlength: '120' });
  const location = h('input', { type: 'text', maxlength: '160', value: row.location_code || '' });
  const room = h('input', { type: 'text', maxlength: '40' });
  const requester = personField('imac-person', row.cpf_no, row.user_name);
  const mobile = h('input', { type: 'text', maxlength: '20', value: (isAdmin() && row.cpf_no ? row.user_mobile : '') || '', placeholder: 'If known' });

  const newAssetWrap = h('div', { hidden: true });
  const newAsset = assetField('imac-new-asset', async (key) => {
    if (key === assetKey) { changeNote.hidden = true; snapshot = { asset_type: row.asset_type, make: row.make, model: row.model, serial_no: row.serial_no, asset_key: assetKey }; refreshSnap(); return; }
    try {
      const d = await get(`/api/registers/assets/${encodeURIComponent(key)}`);
      snapshot = { asset_type: d.row.asset_type, make: d.row.make, model: d.row.model, serial_no: d.row.serial_no, asset_key: key };
      changeNote.hidden = false;
      changeNote.replaceChildren(icon('information--filled'), h('span', null, `This record will show ${assetKey} replaced by ${key}.`));
    } catch (e) { toast(e.message, 'bad'); }
    refreshSnap();
  });
  function refreshSnap() {
    snapCells.type.value = snapshot.asset_type || ''; snapCells.model.value = [snapshot.make, snapshot.model].filter(Boolean).join(' ');
    snapCells.key.value = snapshot.asset_key || ''; snapCells.serial.value = snapshot.serial_no || '';
  }
  changeType.node.addEventListener('change', () => {
    const isChange = changeType.value() === 'CHANGE';
    newAssetWrap.hidden = !isChange;
    if (!isChange) { newAsset.node.querySelector('input').value = ''; changeNote.hidden = true; snapshot = { asset_type: row.asset_type, make: row.make, model: row.model, serial_no: row.serial_no, asset_key: assetKey }; refreshSnap(); }
  });
  newAssetWrap.append(field('New Asset (CI)', newAsset.node));

  const feasible = radioGroup('imac-feasible', [['YES', 'Yes'], ['NO', 'No']], 'YES');
  const reason = h('input', { type: 'text', maxlength: '500', placeholder: 'Required if not feasible' });
  const partName = h('input', { type: 'text', maxlength: '160' });
  const partQty = h('input', { type: 'text', maxlength: '20', value: '1' });
  const partSerial = h('input', { type: 'text', maxlength: '80' });
  const demoGiven = radioGroup('imac-demo', [['YES', 'Yes'], ['NO', 'No']], 'NO');
  const remarks = h('textarea', { rows: '3', maxlength: '1000', placeholder: 'Anything worth noting for this visit' });

  const row4 = (...cells) => h('div', { class: 'frow4' }, ...cells);
  const body = h('div', { class: 'imac-form' },
    h('div', { class: 'rec-note info' }, icon('information--filled'), 'Asset type, model, serial no. and Asset ID are pulled from this asset automatically.'),
    row4(field('Call ticket no.', ticket), field('Date', date), field('ONGC section', section), field('Location', location)),
    row4(field('Room no.', room), field('Requester (name or CPF no.)', requester.node), field('Mobile no.', mobile)),
    h('h3', null, 'Type of work'),
    changeType.node, newAssetWrap, changeNote,
    row4(field('Asset type', snapCells.type), field('Make / model', snapCells.model), field('Asset ID', snapCells.key), field('Serial no.', snapCells.serial)),
    row4(field('Feasible?', feasible.node), field('If no, give reason', reason)),
    h('h3', null, 'Part / software used'),
    row4(field('Name', partName), field('Quantity', partQty), field('Part serial no.', partSerial), field('Demo given', demoGiven.node)),
    field('Remarks', remarks));

  openModal({
    title: `Fill IMAC - ${assetKey}`, wide: true, body,
    actions: [{ label: 'Cancel' }, { label: 'Save and generate PDF', primary: true, icon: 'save', keepOpen: true, onClick: async () => {
      if (feasible.value() === 'NO' && !reason.value.trim()) throw new Error('Give a reason when the work is not feasible.');
      const values = {
        key: assetKey, change_type: changeType.value(), new_asset_key: changeType.value() === 'CHANGE' ? newAsset.value() : '',
        ticket_no: ticket.value.trim(), imac_date: date.value, ongc_section: section.value.trim(), location: location.value.trim(), room_no: room.value.trim(),
        requester_cpf: requester.value(), requester_name: requester.name(), requester_mobile: mobile.value.trim(),
        feasible: feasible.value() === 'YES', infeasible_reason: reason.value.trim(),
        part_name: partName.value.trim(), part_qty: partQty.value.trim(), part_serial_no: partSerial.value.trim(), demo_given: demoGiven.value() === 'YES',
        remarks: remarks.value.trim(),
      };
      const r = await send('/api/edit/assets/imac', values);
      toast('IMAC form saved');
      onDone?.(r.detail);
      try { await downloadGet(`/api/imac/${r.id}/pdf`, null, `IMAC_${assetKey}.pdf`); } catch (e) { toast('Saved, but the PDF could not be downloaded: ' + e.message, 'bad'); }
    } }],
  });
}
