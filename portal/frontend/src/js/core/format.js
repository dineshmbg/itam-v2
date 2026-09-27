const nInt = new Intl.NumberFormat('en-IN');
const nDec = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 });
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export const int = (n) => (n == null ? '—' : nInt.format(n));
export const dec = (n) => (n == null ? '—' : nDec.format(n));
export const pct = (n) => (n == null ? '—' : nDec.format(n) + '%');

export function date(v) {
  if (!v) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v);
  return m ? `${+m[3]} ${MON[+m[2] - 1]} ${m[1]}` : String(v);
}
export function month(v) {
  const m = /^(\d{4})-(\d{2})/.exec(v || '');
  return m ? `${MON[+m[2] - 1]} ${m[1]}` : v;
}
export function clock(ts) { return new Date(ts).toLocaleTimeString('en-GB', { hour12: false }); }
export function humanize(v) {
  if (v == null || v === '') return '—';
  const s = String(v).replace(/_/g, ' ').toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}
export function ago(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  return Math.round(s / 3600) + ' h ago';
}
export function bytes(n) { return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(0) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }
export const titleCase = (s) => String(s).toLowerCase().replace(/(^|[\s-])\S/g, (m) => m.toUpperCase());
/** ALL-CAPS names from the registers read as proper names; mixed-case values are left alone. */
export const person = (s) => (s && s === String(s).toUpperCase() ? titleCase(s) : s);
const ACRONYMS = new Set(['ci', 'cipl', 'ongc', 'os', 'ip', 'ram', 'cpf', 'hr', 'pm', 'dq', 'amc', 'dc', 'rma', 'sr', 'tat', 'sn', 'id', 'mgmt', 'hdd', 'ups', 'oem', 'awb', 'sfp']);
/** Column key -> readable field label, keeping acronyms upper-case: ongc_asset_id -> "ONGC asset ID". */
export function label(key) {
  const words = String(key).split('_').map((w) => (w === 'no' ? 'no.' : ACRONYMS.has(w) ? w.toUpperCase() : w.toLowerCase()));
  const s = words.join(' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}
