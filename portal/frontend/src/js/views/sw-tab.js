// The asset record's "Software" tab: what reports, the check script or an engineer observed on this machine. One row per source, side by side;
// the portal never decides between them. The inspection time is recorded by the server and shown here read-only.
import { h, icon } from '../core/dom.js';
import { label as fieldLabel, when } from '../core/format.js';
import { download, send, upload } from '../core/api.js';
import { openModal } from '../ui/modal.js';
import { toast } from '../ui/toast.js';

const SOURCES = { BIGFIX_REPORT: 'BigFix report', TREND_REPORT: 'Trend Micro report', SCRIPT: 'Check script', MANUAL: 'Entered by hand' };
const FIELDS = ['os_edition', 'os_build', 'os_feature_version', 'trend_installed', 'trend_version', 'trend_status', 'bigfix_installed', 'bigfix_version', 'vpn_name', 'vpn_version', 'vpn_other',
  'sap_gui_version', 'o365_account', 'o365_version', 'last_user', 'connection', 'bitlocker', 'tpm_secureboot', 'office_channel', 'domain_joined', 'last_boot', 'browser_version', 'local_admins',
  'os_product_raw', 'trend_services', 'trend_candidate', 'vpn_service_state', 'vpn_candidate', 'bigfix_service_state', 'o365_status', 'o365_account_source', 'os_update_title', 'os_update_at',
  'defender_signature', 'defender_signature_at', 'chrome_version', 'firefox_version', 'remarks'];

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
  ['domain_joined', 'Domain joined'], ['browser_version', 'Edge version'], ['chrome_version', 'Chrome version'], ['firefox_version', 'Firefox version'], ['remarks', 'Remarks']];

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
  if (sw.updates?.length) {
    out.push(h('div', { class: 'dsec' }, h('h3', null, `Windows update history (${sw.updates.length})`), h('p', { class: 'muted small' }, 'Installation events from the log on the PC itself; a Defender signature update is one of these. The Windows build number in the table above is the real patch level.'),
      h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Installed', 'Update', 'KB'].map((c) => h('th', null, c)))),
        h('tbody', null, sw.updates.map((u) => h('tr', null, h('td', null, when(u.installed_at)), h('td', { class: 'wrap' }, u.title), h('td', { class: 'mono' }, u.kb || '—'))))))));
  }
  if (sw.apps?.length) {
    out.push(h('div', { class: 'dsec' }, h('details', null, h('summary', null, `Installed software seen by the check script (${sw.apps.length})`),
      h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Program', 'Version', 'Publisher', 'Where'].map((c) => h('th', null, c)))),
        h('tbody', null, sw.apps.map((a) => h('tr', null, h('td', { class: 'wrap' }, a.name), h('td', null, a.version || '—'), h('td', { class: 'wrap' }, a.publisher || '—'), h('td', null, a.hive)))))))));
  }
  if (sw.history.length > sw.latest.length) out.push(h('div', { class: 'dsec' }, h('h3', null, `All observations (${sw.history.length})`), table(sw.history)));
  return out;
}

const card = (step, title, ...kids) => h('section', { class: 'sw-card' }, h('span', { class: 'sw-step' }, step), h('h4', null, title), ...kids);

/** Ways to get software data in, as side-by-side cards: [optional first card], fill many machines in Excel, upload files saved by the check script. All or nothing; an engineer can only fill their own machines. */
export function bulkBlock(first) {
  const picks = h('input', { type: 'file', id: 'sw-files', accept: '.txt', multiple: true });
  const saved = h('div', { class: 'sw-out', role: 'status' });
  const goSaved = async () => {
    const files = [...picks.files];
    if (!files.length) { toast('Choose the saved result file(s).', 'bad'); return; }
    const lines = [];
    for (const f of files) {
      try {
        const r = await upload('/api/software/check-upload', f);
        lines.push(h('div', null, h('span', { class: 'badge ' + (r.matched ? 'ok' : 'warn') }, r.matched ? 'Recorded' : 'Not recorded'), ' ', r.computer, r.matched ? '' : ' - ' + (r.reason || 'not matched')));
      } catch (e) { lines.push(h('div', null, h('span', { class: 'badge bad' }, 'Refused'), ' ', f.name, ' - ', e.message || 'unreadable')); }
    }
    saved.replaceChildren(...lines);
  };
  const dl = (path, name, label, ic, title, body = {}) => h('button', { class: 'btn', type: 'button', title, onClick: () => download(path, body, name).catch((e) => toast(e.message, 'bad')) }, icon(ic), label);
  return h('div', { class: 'sw-cards' }, first || null,
    card('Option 2 · by hand, in bulk', 'Enter many machines at once',
      h('p', { class: 'muted' }, 'Every machine with its software and versions, as one Excel file.'),
      h('div', { style: { display: 'flex', justifyContent: 'center' } },      // the button is only as wide as its label, centred in the card
        dl('/api/software/report', 'Software_Report.xlsx', 'Download software report (Excel)', 'document--export', 'Every machine with its Windows, Trend Micro, BigFix, VPN, SAP, Microsoft 365 and browser versions, plus the installed-software list'))),
    card('Option 3 · saved by the script', 'Upload saved check-script results',
      h('p', { class: 'muted' }, 'When a PC could not reach the portal, ankcheck.bat saved ankcheck_<PC name>.txt (next to the script, or in the user Documents folder). Upload those files here; an engineer can upload the PCs assigned to them.'),
      h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'sw-files' }, 'Saved result files (ankcheck_*.txt)'), picks),
      h('div', { class: 'sw-grow' }), h('button', { class: 'btn primary', type: 'button', onClick: goSaved }, icon('upload'), 'Upload results'), saved));
}

export { card as swCard };
