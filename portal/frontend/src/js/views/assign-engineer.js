// The "assign engineer" window - moves many assets to one engineer, optionally only those held by a named old engineer.
import { send } from '../core/api.js';
import { h } from '../core/dom.js';
import { getSchema, toast } from '../core/editor.js';
import { person } from '../core/format.js';
import { openModal } from '../ui/modal.js';

/** keys: asset keys (the listed assets); onDone(result) after a successful save. */
export async function assignEngineerDialog({ keys, onDone }) {
  const schema = await getSchema();
  const opts = (blank) => [h('option', { value: '' }, blank), ...schema.engineers.map((k) => h('option', { value: k }, person(k)))];
  const to = h('select', { id: 'as-to' }, opts('— choose the new engineer —'));
  const from = h('select', { id: 'as-from' }, opts('— any (all listed assets) —'));
  const remarks = h('input', { id: 'as-rem', type: 'text', maxlength: '200', placeholder: 'Optional, kept in the change log' });
  const f = (id, label, el) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), el);
  openModal({
    title: `Assign engineer · ${keys.length} listed asset${keys.length === 1 ? '' : 's'}`,
    lead: 'Pick the new engineer. To hand over one engineer’s assets, also pick the old engineer: only the listed assets currently with them are moved, the rest are left alone.',
    body: h('div', null, f('as-from', 'Old engineer', from), f('as-to', 'New engineer', to), f('as-rem', 'Reason', remarks)),
    actions: [{ label: 'Cancel' }, { label: 'Assign', primary: true, icon: 'checkmark', onClick: async () => {
      if (!to.value) throw new Error('Choose the new engineer.');
      const r = await send('/api/edit/assets/reassign', { asset_keys: keys, engineer: to.value, from_engineer: from.value, reason: remarks.value });
      toast(`${r.changed} asset${r.changed === 1 ? '' : 's'} assigned to ${person(to.value)}${r.skipped ? ` · ${r.skipped} left as they were` : ''}`);
      onDone?.(r);
    } }],
  });
}
