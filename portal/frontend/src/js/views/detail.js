import { h, icon } from '../core/dom.js';
import { date, dec, int, label as fieldLabel, person } from '../core/format.js';
import { href } from '../core/router.js';
import { badge } from '../ui/badge.js';
import { entity } from '../ui/hovercard.js';

const REF = { asset_key: 'asset', parent_asset_key: 'asset', sr_id: 'call', call_sr_id: 'call', cpf_no: 'cpf', engineer: 'engineer', engineer_name: 'engineer', pm_done_by: 'engineer' };

const BADGE_KEYS = new Set(['asset_status', 'cover_status', 'pm_status', 'call_status', 'priority', 'spare_status', 'return_status', 'faulty_spare_status']);
const NAME_KEYS = new Set(['user_name', 'engineer', 'engineer_name', 'site_incharge', 'pm_done_by', 'pm_signed_by', 'received_by', 'user_designation']);
const MONO = /(_key|serial|_no$|_id$|^ip_|ip_address|hostname|sr_id|^ci_)/;

const ASSET_GROUPS = [
  ['Identity', ['asset_key', 'ci_no', 'asset_class', 'asset_type', 'record_level', 'parent_asset_key', 'hostname', 'asset_description']],
  ['Hardware', ['make', 'model', 'serial_no', 'ongc_asset_id', 'ongc_census_no', 'sub_type', 'os', 'os_family', 'firmware_version', 'ip_address', 'mgmt_ip', 'processor', 'ram', 'storage', 'attached_monitor_model', 'attached_monitor_sn', 'ports', 'network_port_available', 'capacity', 'battery_qty', 'battery_spec', 'install_date']],
  ['Ownership', ['cpf_no', 'user_name', 'user_designation', 'user_level', 'user_department', 'user_hr_status', 'user_category', 'user_retirement_date']],
  ['Location and care', ['location_code', 'floor_area', 'room', 'engineer_name']],
  ['Contract', ['cover_type', 'cover_expiry_date', 'cover_status', 'rate_component', 'rate_value']],
  ['Lifecycle', ['purchase_date', 'purchase_cost', 'vendor_name', 'po_no', 'refresh_due_date']],
  ['Preventive maintenance', ['pm_quarter', 'pm_date', 'pm_status', 'pm_done_by', 'pm_signed_by', 'pm_tracker_date']],
  ['Status', ['asset_status', 'remarks', 'extra_info']],
];

const RELATED = {
  assets: [['components', 'Components', 'assets', ['id', 'asset_type', 'model', 'serial_no', 'asset_status']], ['calls', 'Calls', 'calls', ['id', 'cipl_call_date', 'problem_description', 'engineer', 'call_status']],
    ['inward', 'Inward', 'inward', ['id', 'inward_date', 'part_description', 'received_date']], ['outward', 'Outward', 'outward', ['id', 'outward_date', 'part_description', 'gatepass_no', 'sent_date']],
    ['rma', 'OEM RMA', 'rma', ['id', 'rma_no', 'fault_item', 'call_log_date', 'return_status']], ['verifications', 'Physical checks', null, ['verified_on', 'result', 'verified_by', 'note']], ['serial_history', 'Serial history (RMA swaps)', null, ['old_serial', 'new_serial', 'rma_no', 'change_date']],
    ['history', 'Change history', null, ['snapshot_date', 'change_type', 'field', 'old_value', 'new_value']]],
  calls: [['inward', 'Inward', 'inward', ['id', 'inward_date', 'part_description', 'received_date']], ['outward', 'Outward', 'outward', ['id', 'outward_date', 'part_description', 'gatepass_no', 'sent_date']],
    ['rma', 'OEM RMA', 'rma', ['id', 'rma_no', 'fault_item', 'return_status']], ['history', 'Change history', null, ['snapshot_date', 'change_type', 'field', 'old_value', 'new_value']]],
  rma: [['serial_history', 'Serial history (RMA swaps)', null, ['old_serial', 'new_serial', 'rma_no', 'change_date']]],
};
const SINGLE = { assets: [], calls: [['asset', 'Asset', 'assets']], inward: [['asset', 'Asset', 'assets'], ['call', 'Call', 'calls']], outward: [['asset', 'Asset', 'assets'], ['call', 'Call', 'calls']], rma: [['asset', 'Asset', 'assets'], ['call', 'Call', 'calls']] };

export function value(key, v, self) {
  // Hostname is blank on most assets in the source data - show the asset's own CI number in its place rather than a bare dash,
  // so the field always reads as something. `self` is the asset's own key wherever this is used (see renderDetail below).
  if (key === 'hostname' && (v == null || v === '') && self) return h('span', { class: 'mono faint', title: 'No hostname recorded - showing the Asset (CI) number' }, self);
  if (v == null || v === '') return h('span', { class: 'faint' }, '—');
  if (REF[key] && !(self && v === self)) return entity(REF[key], v, NAME_KEYS.has(key) ? person(String(v)) : String(v), { mono: !NAME_KEYS.has(key) });
  if (BADGE_KEYS.has(key)) return badge(v);
  if (key.endsWith('_date') || key === 'date') return date(v);
  if (typeof v === 'number') return key.endsWith('_days') || key === 'rate_value' || key === 'purchase_cost' ? dec(v) : (key.endsWith('_no') || key === 'cpf_no' ? String(v) : int(v));
  if (NAME_KEYS.has(key)) return person(String(v));
  if (MONO.test(key)) return h('span', { class: 'mono' }, v);
  return String(v);
}

function override(key, o, ctx) {
  if (!o) return null;
  const differs = o.source !== undefined && o.source !== null && String(o.source) !== '' && ctx.row && String(o.source) !== String(ctx.row[key] ?? '');
  return h('span', { class: 'ovr', title: `Set manually by ${o.editor} on ${date(o.at.slice(0, 10))}. Loaders keep this value.` },
    icon('locked'), differs ? h('span', { class: 'ovr-src' }, `source file: ${String(o.source)}`) : null,
    ctx.onReset && ctx.canEdit?.(key) ? h('button', { class: 'link-btn', type: 'button', onClick: () => ctx.onReset(key), title: 'Go back to the value in the source file' }, 'Reset') : null);
}

function kvBlock(keys, row, ctx = {}) {
  const ov = ctx.overrides || {};
  const items = keys.filter((k) => k in row).flatMap((k) => [h('div', { class: 'k' }, fieldLabel(k)), h('div', { class: 'v' }, value(k, row[k], ctx.self), override(k, ov[k], { ...ctx, row }))]);
  return items.length ? h('div', { class: 'kv' }, items) : null;
}

function relTable(rows, cols, ds) {
  return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, cols.map((c) => h('th', null, c === 'id' ? 'ID' : fieldLabel(c))))),
    h('tbody', null, rows.map((r) => h('tr', null, cols.map((c) => {
      const v = r[c];
      if (c === 'id' && ds) return h('td', null, h('a', { class: 'mono', href: href('registers/' + ds, { open: v }) }, v));
      return h('td', { class: c === 'problem_description' || c === 'part_description' ? 'wrap' : '' }, value(c, v));
    }))))));
}

export function renderDetail(ds, payload, ctx = {}) {
  const { row, related } = payload;
  ctx = { self: payload.id, ...ctx };
  const out = [];
  if (row.dq_flags) out.push(h('div', { class: 'dsec' }, h('h3', null, 'Data-quality flags'), h('div', { class: 'flags' }, row.dq_flags.split('; ').map((f) => h('span', { class: 'badge warn' }, icon('warning--alt--filled'), f.replace(/_/g, ' ').toLowerCase())))));
  if (ds === 'assets') {
    ASSET_GROUPS.forEach(([title, keys]) => { const b = kvBlock(keys, row, ctx); if (b) out.push(h('div', { class: 'dsec' }, h('h3', null, title), b)); });
  } else {
    const keys = Object.keys(row).filter((k) => k !== 'dq_flags');
    out.push(h('div', { class: 'dsec' }, h('h3', null, 'Details'), kvBlock(keys, row, ctx)));
  }
  (SINGLE[ds] || []).forEach(([k, title, target]) => {
    const r = related[k];
    if (!r) return;
    const keys = Object.keys(r).filter((x) => x !== 'id');
    out.push(h('div', { class: 'dsec' }, h('h3', null, title), h('div', { class: 'kv' }, [h('div', { class: 'k' }, 'Open'), h('div', { class: 'v' }, h('a', { class: 'mono', href: href('registers/' + target, { open: r.id }) }, r.id)),
      ...keys.flatMap((x) => [h('div', { class: 'k' }, fieldLabel(x)), h('div', { class: 'v' }, value(x, r[x]))])])));
  });
  (RELATED[ds] || []).forEach(([k, title, target, cols]) => {
    const rows = related[k];
    if (!rows || !rows.length) return;
    out.push(h('div', { class: 'dsec' }, h('h3', null, `${title} (${rows.length})`), relTable(rows, cols, target)));
  });
  return out;
}
