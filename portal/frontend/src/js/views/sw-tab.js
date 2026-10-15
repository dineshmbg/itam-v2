// The asset record's "Software" tab: what reports, the check script or an engineer observed on this machine. One row per source, side by side;
// the portal never decides between them. The inspection time is recorded by the server and shown here read-only.
import { h, icon } from '../core/dom.js';
import { label as fieldLabel, when } from '../core/format.js';
import { download, send, upload } from '../core/api.js';
import { openModal } from '../ui/modal.js';
import { toast } from '../ui/toast.js';

const SOURCES = { BIGFIX_REPORT: 'BigFix report', TREND_REPORT: 'Trend Micro report', SCRIPT: 'Check script', MANUAL: 'Entered by hand' };
const FIELDS = ['os_edition', 'os_build', 'os_feature_version', 'trend_installed', 'trend_version', 'trend_status', 'bigfix_installed', 'bigfix_version', 'vpn_name', 'vpn_version', 'vpn_other',
  'sap_gui_version', 'o365_account', 'o365_version', 'last_user', 'connection', 'bitlocker', 'tpm_secureboot', 'office_channel', 'domain_joined', 'last_boot', 'browser_version', 'local_admins', 'remarks'];

const cell = (v) => (v === true ? 'Yes' : v === false ? 'No' : v == null || v === '' ? h('span', { class: 'faint' }, '—') : String(v));

function table(rows) {
  const cols = FIELDS.filter((f) => rows.some((r) => r[f] != null && r[f] !== ''));
  return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
    h('thead', null, h('tr', null, ['Source', 'Inspected', 'Data as of', ...cols.map(fieldLabel), 'Notes'].map((c) => h('th', null, c)))),
    h('tbody', null, rows.map((r) => h('tr', null,
      h('td', null, SOURCES[r.source] || r.source),
      h('td', null, when(r.inspected_at)),
      h('td', null, r.data_as_of ? when(r.data_as_of) : h('span', { class: 'faint' }, '—')),
      cols.map((c) => h('td', null, cell(r[c]))),
      h('td', { class: 'wrap' }, (r.flags || []).map((f) => h('div', { class: 'badge warn' }, f))))))));
}

const YN = ['trend_installed', 'bigfix_installed', 'vpn_other', 'bitlocker', 'tpm_secureboot', 'domain_joined'];
const FORM = [['os_edition', 'Windows edition'], ['os_build', 'Windows build'], ['os_feature_version', 'Feature version (e.g. 25H2)'], ['trend_installed', 'Trend Micro installed'], ['trend_version', 'Trend Micro version'],
  ['bigfix_installed', 'BigFix installed'], ['bigfix_version', 'BigFix version'], ['vpn_name', 'VPN (Zscaler or other)'], ['vpn_version', 'VPN version'], ['sap_gui_version', 'SAP GUI version'],
  ['o365_account', 'Microsoft 365 account'], ['o365_version', 'Office version'], ['last_user', 'Last logged-on user'], ['bitlocker', 'BitLocker on'], ['tpm_secureboot', 'TPM / Secure Boot on'],
  ['domain_joined', 'Domain joined'], ['remarks', 'Remarks']];

/** Type in what you saw on this machine. Saved as an observation "Entered by hand"; the date is recorded by the server. */
export function addObservationDialog(key, done) {
  const inputs = Object.fromEntries(FORM.map(([f]) => [f, YN.includes(f)
    ? h('select', { id: 'swo-' + f }, ['', 'Yes', 'No'].map((v) => h('option', { value: v }, v || 'Not checked')))
    : h('input', { id: 'swo-' + f, type: 'text', maxlength: '200' })]));
  openModal({
    title: `Add observation for ${key}`, lead: 'Fill only what you checked; leave the rest blank (blank is not the same as No). The date is recorded automatically and cannot be changed.', large: true,
    body: h('div', null, FORM.map(([f, label]) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'swo-' + f }, label), inputs[f]))),
    actions: [{ label: 'Cancel' }, { label: 'Save observation', primary: true, onClick: async () => {
      const facts = Object.fromEntries(FORM.map(([f]) => [f, inputs[f].value.trim()]).filter(([, v]) => v));
      if (!Object.keys(facts).length) throw new Error('Enter at least one value.');
      const r = await send('/api/software/manual', { key, facts });
      toast('Observation saved'); done(r.software);
    } }],
  });
}

export function softwareTab(sw, add) {
  const out = [add ? h('div', { class: 'btn-row' }, h('button', { class: 'btn', type: 'button', onClick: add }, icon('add'), 'Add observation')) : null, h('p', { class: 'muted' }, 'The inspection time is recorded automatically when the information arrives and cannot be changed. Each source is kept as it reported; where they disagree, the engineer decides.')].filter(Boolean);
  if (!sw.latest.length) return [...out, h('div', { class: 'dsec' }, h('p', { class: 'faint' }, 'Nothing has been observed on this machine yet.'))];
  if (sw.conflicts.length) {
    out.push(h('div', { class: 'rec-note warn', role: 'status' }, icon('warning'), h('div', null, h('strong', null, 'Sources disagree'),
      sw.conflicts.map((c) => h('div', null, `${c.field}: ` + Object.entries(c.values).map(([s, v]) => `${SOURCES[s] || s} says ${v}`).join('; '))))));
  }
  out.push(h('div', { class: 'dsec' }, h('h3', null, `Latest by source (${sw.latest.length})`), table(sw.latest)));
  if (sw.history.length > sw.latest.length) out.push(h('div', { class: 'dsec' }, h('h3', null, `All observations (${sw.history.length})`), table(sw.history)));
  return out;
}

/** Fill in many machines at once: download the Excel template, upload it filled. All or nothing; an engineer can only fill their own machines. */
export function bulkBlock() {
  const file = h('input', { type: 'file', id: 'sw-bulk', accept: '.xlsx' });
  const out = h('div', { class: 'stack-v', role: 'status' });
  const go = async () => {
    if (!file.files[0]) { toast('Choose the filled template.', 'bad'); return; }
    try {
      const r = await upload('/api/software/bulk', file.files[0]);
      out.replaceChildren(r.errors.length ? h('div', null, h('span', { class: 'badge bad' }, 'Nothing saved'), h('ul', null, r.errors.slice(0, 30).map((t) => h('li', null, t))))
        : h('div', null, h('span', { class: 'badge ok' }, 'Saved'), ` ${r.recorded} machine(s) recorded.`));
    } catch (e) { out.replaceChildren(h('div', null, h('span', { class: 'badge bad' }, 'Refused'), ' ', e.message || 'unreadable')); }
  };
  return h('div', { class: 'stack-v' },
    h('p', { class: 'muted' }, 'Fill in what you checked on many machines at once. One row per machine; blank means not checked. If any row has a problem nothing is saved, and the problem rows are listed.'),
    h('div', { class: 'btn-row' }, h('button', { class: 'btn', type: 'button', onClick: () => download('/api/software/template', {}, 'Software_Observation_Upload_Template.xlsx').catch((e) => toast(e.message, 'bad')) }, icon('download'), 'Download Excel template')),
    h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'sw-bulk' }, 'Filled template (.xlsx)'), file),
    h('div', { class: 'btn-row' }, h('button', { class: 'btn primary', type: 'button', onClick: go }, icon('upload'), 'Upload filled template')),
    out);
}
