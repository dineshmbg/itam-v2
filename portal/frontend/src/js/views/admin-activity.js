import { h, icon } from '../core/dom.js';
import { mountListPage } from './list-page.js';
import { when } from '../core/format.js';

const detailText = (d) => (d == null ? '' : typeof d === 'string' ? d : Object.entries(d).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · '));

export function mountActivity(root) {
  return mountListPage(root, {
    title: 'Activity log', sub: 'Everything people do in the portal: sign-ins, pages opened, changes, downloads, imports, backups and administration.', endpoint: '/api/admin/activity', liveTables: ['portal_activity'],
    facetDefs: [{ key: 'user', label: 'User' }, { key: 'action', label: 'Activity' }, { key: 'hostname', label: 'Hostname' }],
    columns: [
      { label: 'When', render: (r) => when(r.at), cls: 'nowrap' },
      { label: 'User', render: (r) => r.username || '—' },
      { label: 'Activity', render: (r) => h('span', { class: 'badge ' + (r.ok ? 'mute' : 'bad') }, r.ok ? null : icon('warning--alt--filled'), r.action.replace(/_/g, ' ').toLowerCase()) },
      { label: 'Target', cls: 'wrap', render: (r) => h('span', { class: 'mono' }, r.target || '') },
      { label: 'Detail', cls: 'wrap', render: (r) => detailText(r.detail) },
      { label: 'From', render: (r) => h('span', null, h('span', { class: 'mono faint' }, r.ip || ''), r.hostname ? h('span', { class: 'faint', style: { display: 'block', fontSize: '11px' } }, r.hostname) : null) },
      { label: 'Result', render: (r) => (r.ok ? h('span', { class: 'tick' }, icon('checkmark--filled'), h('span', { class: 'sr-only' }, 'Succeeded')) : h('span', { class: 'cross' }, icon('close--filled'), h('span', { class: 'sr-only' }, 'Failed'))) },
    ],
  });
}
