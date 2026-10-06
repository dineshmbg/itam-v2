// Who is signed in, and keeping the session honest: keep-alive while the person is active, a warning before the idle time-out, sign-out on expiry.
import { get, onAuthLost, send } from './api.js';

let me = { authenticated: false };
const subs = new Set();
let idleTimeout = 900;          // seconds, from the server
let lastActivity = Date.now();
let lastPing = 0;
let ticker = null;
let warned = false;
let hooks = { warn: () => {}, expired: () => {}, clearWarn: () => {} };

export const user = () => me.user || null;
export const isAdmin = () => !!me.is_admin;
/** A real administrator - a User with extended access passes isAdmin() but not this (no Administration, no Software update). */
export const isFullAdmin = () => !!me.is_full_admin;
/** A plain User whose designation is Team Leader/SI: gets PM cycles, Inventory match, Users and Activity log (never Data import, Backup, Software update, E-mail, Control). */
export const hasLeadTools = () => !!me.has_lead_tools;
/** A non-admin engineer individually granted into Calls/Inward/Outward/OEM RMA (see Manage user -> Module access). */
export const callPartsAccess = () => (isAdmin() ? 'FULL' : me.user?.call_parts_access || 'NONE');
export const hasCallPartsAccess = () => callPartsAccess() !== 'NONE';
export const state = () => me.state || null;
export const current = () => me;
export const onSession = (fn) => { subs.add(fn); return () => subs.delete(fn); };
const emit = () => subs.forEach((fn) => fn(me));

export function setHooks(h) { hooks = { ...hooks, ...h }; }

function apply(next) {
  me = next;
  if (me.idle_timeout_s) idleTimeout = me.idle_timeout_s;
  emit();
}

export async function load() {
  apply(await get('/api/auth/me'));
  return me;
}

export async function signIn(username, password) {
  apply(await send('/api/auth/login', { username, password }));
  markActive(true);
  return me;
}
export async function secondFactor(code) {
  apply(await send('/api/auth/totp', { code }));
  markActive(true);
  return me;
}
export async function changePassword(current, next) {
  apply(await send('/api/auth/password', { current, new: next }));
  return me;
}
export async function signOut(reason) {
  stop();
  try { await send('/api/auth/logout', {}); } catch (_) { /* already gone */ }
  apply({ authenticated: false, reason });
}

// ---- activity tracking
export function markActive(force = false) {
  lastActivity = Date.now();
  if (warned) { warned = false; hooks.clearWarn(); }
  if (force || Date.now() - lastPing > 60000) ping();
}

async function ping() {
  if (me.state !== 'ok') return;
  lastPing = Date.now();
  try { await send('/api/auth/keepalive', {}); } catch (_) { /* a 401 is handled by onAuthLost */ }
}

export function start() {
  stop();
  lastActivity = Date.now(); lastPing = Date.now();
  const on = () => markActive();
  ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach((ev) => window.addEventListener(ev, on, { passive: true }));
  ticker = setInterval(() => {
    if (me.state !== 'ok') return;
    const idle = (Date.now() - lastActivity) / 1000;
    const left = idleTimeout - idle;
    if (left <= 0) { hooks.clearWarn(); signOut('idle'); hooks.expired(); }
    else if (left <= 60 && !warned) { warned = true; hooks.warn(() => Math.max(0, Math.round(idleTimeout - (Date.now() - lastActivity) / 1000))); }
  }, 1000);
  start.off = () => ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach((ev) => window.removeEventListener(ev, on));
}
export function stop() {
  if (ticker) clearInterval(ticker);
  ticker = null; warned = false;
  start.off?.();
}

onAuthLost(() => {
  if (me.authenticated) { stop(); apply({ authenticated: false, reason: 'expired' }); }
});
