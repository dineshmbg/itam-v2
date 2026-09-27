// The "record preventive maintenance" window - for one asset (from its drawer) or for many (from the PM worklist).
import { send } from '../core/api.js';
import { h } from '../core/dom.js';
import { getSchema, toast } from '../core/editor.js';
import { person } from '../core/format.js';
import { openModal } from '../ui/modal.js';

const today = () => new Date().toISOString().slice(0, 10);

/** keys: asset keys; defaults: {engineer}; onDone(result) after a successful save. */
export async function recordPmDialog({ keys, defaults = {}, onDone }) {
  const schema = await getSchema();
  const date = h('input', { id: 'pm-date', type: 'date', value: today(), max: today() });
  const by = h('select', { id: 'pm-by' }, [h('option', { value: '' }, '— not recorded —'), ...schema.engineers.map((k) => h('option', { value: k }, person(k)))]);
  by.value = defaults.engineer || '';
  const signed = h('input', { id: 'pm-signed', type: 'text', maxlength: '80', placeholder: 'Name of the ONGC officer who signed off', value: defaults.signed || '' });
  const remarks = h('input', { id: 'pm-rem', type: 'text', maxlength: '200', placeholder: 'Optional' });
  const f = (id, label, el) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), el);
  openModal({
    title: keys.length === 1 ? `Record PM · ${keys[0]}` : `Record PM for ${keys.length} assets`,
    lead: 'The date must fall inside the current quarter. Status, PM flags and the quarter picture update at once.',
    body: h('div', null, f('pm-date', 'PM done on', date), f('pm-by', 'Done by', by), f('pm-signed', 'Signed off by', signed), f('pm-rem', 'Remarks', remarks)),
    actions: [{ label: 'Cancel' }, { label: 'Record PM', primary: true, icon: 'checkmark', onClick: async () => {
      const r = await send('/api/pm/record', { asset_keys: keys, pm_date: date.value, done_by: by.value, signed_by: signed.value, remarks: remarks.value });
      toast(`PM recorded for ${r.recorded} asset${r.recorded === 1 ? '' : 's'}`);
      onDone?.(r);
    } }],
  });
}
