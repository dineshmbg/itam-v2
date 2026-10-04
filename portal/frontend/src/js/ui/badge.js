import { h, icon } from '../core/dom.js';
import { humanize } from '../core/format.js';

// value -> [tone, icon, label]. Status is always icon + text (never colour alone).
const MAP = {
  // asset status
  IN_USE: ['ok', 'checkmark--filled', 'Deployed'], IN_STORE: ['info', 'box', 'In stock'], SURPLUS: ['mute', 'archive', 'Surplus'],
  NOT_IN_USE: ['mute', 'circle-dash', 'Not in use'], NOT_ON_NETWORK: ['warn', 'warning--alt--filled', 'Offline'],
  STANDBY: ['info', 'pending', 'Standby'], REMOVED_FROM_AMC: ['bad', 'close--filled', 'Out of AMC'], TRANSFERRED: ['mute', 'arrow--right', 'Transferred'],
  // cover
  ACTIVE: ['ok', 'checkmark--filled', 'Active'], EXPIRING_90D: ['warn', 'warning--alt--filled', 'Renewal due'], EXPIRED: ['bad', 'error--filled', 'Lapsed'],
  REMOVED: ['mute', 'close--outline', 'Removed'], UNKNOWN: ['mute', 'circle-dash', 'Unknown'],
  // pm
  DONE: ['ok', 'checkmark--filled', 'Completed'], DONE_OUTSIDE_QUARTER: ['warn', 'warning--alt--filled', 'Completed late'], PENDING: ['warn', 'pending', 'Scheduled'],
  NA: ['mute', 'circle-dash', 'n/a'], NOT_TRACKED: ['mute', 'circle-dash', 'Not tracked'],
  // calls
  OPEN: ['warn', 'in-progress', 'Raised'], CLOSED: ['ok', 'checkmark--filled', 'Resolved'],
  P1: ['bad', 'error--filled', 'P1'], P2: ['info', 'information--filled', 'P2'], P3: ['mute', 'circle-dash', 'P3'],
  NO_SPARE_NEEDED: ['mute', 'circle-dash', 'No part required'], PART_RECEIVED: ['ok', 'checkmark--filled', 'Part received'], PART_PENDING: ['warn', 'hourglass', 'Awaiting part'],
  // rma / engineers
  RETURNED: ['ok', 'checkmark--filled', 'Returned'], COMPLETE: ['ok', 'checkmark--filled', 'Complete'], PARTIAL: ['warn', 'in-progress', 'Partial'], NOT_STARTED: ['bad', 'circle-dash', 'Not started'],
  LEFT_ROSTER: ['warn', 'warning--alt--filled', 'Left roster'], RESIGNED: ['mute', 'close--outline', 'Resigned'],
};

// Wording that depends on the column: the same stored code means something different in a different register (PENDING is a PM
// status on an asset, but an RMA's faulty part still awaiting return). Looked up first; falls back to MAP.
const BY_COLUMN = {
  return_status: { PENDING: ['warn', 'pending', 'Awaiting return'] },
  faulty_spare_status: { SENT: ['ok', 'checkmark--filled', 'Returned to OEM'], NOT_SENT: ['warn', 'pending', 'Not yet returned'] },
};
const pick = (value, key) => (key && BY_COLUMN[key] && BY_COLUMN[key][value]) || MAP[value];

export function badge(value, key) {
  if (value == null || value === '') return h('span', { class: 'faint' }, '—');
  const m = pick(value, key);
  if (!m) return h('span', { class: 'badge mute' }, humanize(value));
  return h('span', { class: 'badge ' + m[0] }, icon(m[1]), m[2]);
}

export function tickCross(ok, yesText, noText) {
  return h('span', { class: ok ? 'tick' : 'cross', title: ok ? yesText : noText }, icon(ok ? 'checkmark--filled' : 'close--filled'), h('span', { class: 'sr-only' }, ok ? yesText : noText));
}

export function genderGlyph(g) {
  if (g === 'M') return h('span', { class: 'gender', title: 'Male' }, icon('gender--male', 'lg'), h('span', { class: 'sr-only' }, 'Male'));
  if (g === 'F') return h('span', { class: 'gender', title: 'Female' }, icon('gender--female', 'lg'), h('span', { class: 'sr-only' }, 'Female'));
  return h('span', { class: 'gender faint', title: 'Not recorded' }, icon('user', 'lg'), h('span', { class: 'sr-only' }, 'Gender not recorded'));
}

export const TONE_VAR = { ok: '--c-ok', warn: '--c-warn', bad: '--c-bad', info: '--c-info', mute: '--c-text-3' };
export const toneOf = (value) => (MAP[value] ? MAP[value][0] : 'mute');
export const labelOf = (value, key) => { const m = pick(value, key); return m ? m[2] : humanize(value); };

// asset_class -> --chart-N: a stable colour per class (see edit.py CLASSES), used only as a small identifying dot next to the
// class name (register table, hover card) - never as the badge's own background, so it stays legible at hundreds of rows.
const CLASS_VAR = {
  LAPTOP: '--chart-1', DESKTOP: '--chart-9', WORKSTATION: '--chart-6', SWITCH: '--chart-3', ROUTER: '--chart-10',
  MEDIA_CONVERTER: '--chart-7', PRINTER: '--chart-2', SCANNER: '--chart-4', UPS: '--chart-5', SERVER: '--chart-8',
};
export const classDotVar = (value) => CLASS_VAR[value] || null;
