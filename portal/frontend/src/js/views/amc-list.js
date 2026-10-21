// Shared pieces of the remove-from-AMC pages: the "paste or upload a list" checker and the request status chip.
import { send, upload } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { int } from '../core/format.js';
import { toast } from '../ui/toast.js';

export const STATUS = {
  REQUESTED: ['warn', 'pending', 'Waiting for a decision'], PENDING: ['info', 'time', 'Approved - waits for its date'], APPLIED: ['ok', 'checkmark--filled', 'Removed'],
  REJECTED: ['bad', 'close--filled', 'Turned down'], CANCELLED: ['mute', 'close--outline', 'Withdrawn'], REVERSED: ['mute', 'renew', 'Reinstated'],
};
export const chip = (tone, ico, text) => h('span', { class: 'badge ' + tone }, icon(ico), text);
export const statusChip = (s) => (STATUS[s] ? chip(...STATUS[s]) : chip('mute', 'circle-dash', s));

const RESULT = { OK: ['ok', 'checkmark--filled', 'Can be removed'], BLOCKED: ['bad', 'close--filled', 'Cannot be removed'], NOT_FOUND: ['warn', 'warning--alt--filled', 'Not found'], AMBIGUOUS: ['warn', 'warning--alt--filled', 'More than one asset'] };

/** A fold-out box: paste names or upload a file, see what each one matched, then add the ones that can go. onAdd(rows) gets the matched asset rows. */
export function listChecker({ onAdd }) {
  const text = h('textarea', { rows: '4', placeholder: 'One per line (or separated by commas): asset (CI) number, hostname, serial number or ONGC asset ID', 'aria-label': 'Asset names to check' });
  const file = h('input', { type: 'file', accept: '.txt,.csv,.xlsx', 'aria-label': 'A file with asset names' });
  const out = h('div');
  async function show(promise) {
    try { draw(await promise); } catch (e) { toast(e.message, 'bad'); }
  }
  function draw(res) {
    const good = res.rows.filter((r) => r.result === 'OK');
    out.replaceChildren(
      h('p', { class: 'small', role: 'status' }, `${int(res.rows.length)} checked: `, h('strong', null, `${int(res.ok)} can be removed`), `, ${int(res.blocked)} cannot, ${int(res.not_found)} not found, ${int(res.ambiguous)} unclear.`),
      h('div', { class: 'tbl-wrap', style: { maxHeight: '40vh', overflow: 'auto' } }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['You entered', 'Result', 'Asset / reason'].map((t) => h('th', null, t)))),
        h('tbody', null, res.rows.map((r) => h('tr', null, h('td', { class: 'mono' }, r.input), h('td', null, chip(...RESULT[r.result])),
          h('td', null, r.row ? h('span', null, h('span', { class: 'mono' }, r.row.asset_key), ' ', [r.row.make, r.row.model].filter(Boolean).join(' ')) : null, r.why ? h('div', { class: 'muted small' }, r.why) : null)))))),
      h('div', { class: 'btn-row', style: { marginTop: '8px' } }, h('button', { class: 'btn primary', type: 'button', disabled: !good.length, onClick: () => { onAdd(good.map((r) => r.row)); toast(`${good.length} added`); } },
        icon('add'), `Add the ${int(good.length)} that can be removed`)));
  }
  file.addEventListener('change', () => { if (file.files[0]) show(upload('/api/amc-removal/match-file', file.files[0])); });
  const el = h('details', { class: 'frow' }, h('summary', null, 'Have a list? Paste it or upload a file'), text,
    h('div', { class: 'btn-row', style: { margin: '8px 0' } }, h('button', { class: 'btn', type: 'button', onClick: () => { if (text.value.trim()) show(send('/api/amc-removal/match', { text: text.value })); } }, icon('search'), 'Check the list'),
      h('label', { class: 'muted small' }, 'or a file (.txt, .csv, .xlsx - first column): ', file)), out);
  return el;
}
