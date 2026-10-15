// The asset record's "Software" tab: what reports, the check script or an engineer observed on this machine. One row per source, side by side;
// the portal never decides between them. The inspection time is recorded by the server and shown here read-only.
import { h, icon } from '../core/dom.js';
import { label as fieldLabel, when } from '../core/format.js';

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

export function softwareTab(sw) {
  const out = [h('p', { class: 'muted' }, 'The inspection time is recorded automatically when the information arrives and cannot be changed. Each source is kept as it reported; where they disagree, the engineer decides.')];
  if (!sw.latest.length) return [...out, h('div', { class: 'dsec' }, h('p', { class: 'faint' }, 'Nothing has been observed on this machine yet.'))];
  if (sw.conflicts.length) {
    out.push(h('div', { class: 'rec-note warn', role: 'status' }, icon('warning'), h('div', null, h('strong', null, 'Sources disagree'),
      sw.conflicts.map((c) => h('div', null, `${c.field}: ` + Object.entries(c.values).map(([s, v]) => `${SOURCES[s] || s} says ${v}`).join('; '))))));
  }
  out.push(h('div', { class: 'dsec' }, h('h3', null, `Latest by source (${sw.latest.length})`), table(sw.latest)));
  if (sw.history.length > sw.latest.length) out.push(h('div', { class: 'dsec' }, h('h3', null, `All observations (${sw.history.length})`), table(sw.history)));
  return out;
}
