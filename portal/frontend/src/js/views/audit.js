import { h } from '../core/dom.js';
import { date, label as fieldLabel } from '../core/format.js';
import { entity } from '../ui/hovercard.js';
import { mountListPage } from './list-page.js';

const short = (v) => (v == null || v === '' ? '—' : String(v));
const ACTION = { UPDATE: 'Changed', CREATE: 'Created', ARCHIVE: 'Archived', RESTORE: 'Restored', RESET: 'Override removed', CASCADE: 'Follow-on update', EVENT: 'Event' };
const KIND = { assets: 'asset', calls: 'call' };

export const when = (iso) => {
  const d = new Date(iso);
  return isNaN(d) ? iso : `${date(iso.slice(0, 10))} ${d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`;
};

export function mountAudit(root) {
  return mountListPage(root, {
    title: 'Change log', sub: 'Every change made to a record from the portal: who, when, from where, old and new value.', endpoint: '/api/audit', liveTables: ['portal_audit'],
    facetDefs: [{ key: 'dataset', label: 'Register' }, { key: 'action', label: 'Action' }, { key: 'editor', label: 'Changed by' }],
    columns: [
      { label: 'When', render: (r) => when(r.at), cls: 'nowrap' },
      { label: 'By', render: (r) => r.editor },
      { label: 'Action', render: (r) => h('span', { class: 'badge mute' }, ACTION[r.action] || r.action) },
      { label: 'Register', render: (r) => fieldLabel(r.dataset) },
      { label: 'Record', render: (r) => (KIND[r.dataset] ? entity(KIND[r.dataset], r.record_key) : h('span', { class: 'mono' }, r.record_key)) },
      { label: 'Change', cls: 'wrap', render: (r) => h('div', null, Object.entries(r.changes || {}).filter(([, v]) => v && typeof v === 'object' && 'new' in v).map(([k, v]) => h('div', null, h('span', { class: 'muted' }, fieldLabel(k) + ': '), short(v.old), ' → ', h('strong', null, short(v.new))))) },
      { label: 'Reason', cls: 'wrap', render: (r) => r.reason || '' },
      { label: 'From', render: (r) => h('span', { class: 'mono faint' }, r.client_ip || '') },
    ],
  });
}
