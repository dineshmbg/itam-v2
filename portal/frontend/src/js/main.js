import { send } from './core/api.js';
import { $, clear, h } from './core/dom.js';
import * as live from './core/live.js';
import * as router from './core/router.js';
import * as session from './core/session.js';
import { buildShell } from './ui/shell.js';
import { destroyAll } from './ui/charts.js';
import { closeDrawer } from './ui/drawer.js';
import { initHoverCards } from './ui/hovercard.js';
import { closeAllModals, openModal } from './ui/modal.js';
import { mountLogin, passwordDialog, twoFactorSetup } from './views/login.js';

const app = $('#app');
let shell = null;
let view = null;
let running = false;
let lastPath = '';

const TITLES = { pm: 'Preventive maintenance', cycles: 'PM cycles', history: 'Past quarters', reports: 'Report builder', import: 'Data import', backup: 'Backup and restore', update: 'Software update', email: 'E-mail and alerts', assets: 'Assets', calls: 'Call tracker', engineers: 'Engineers', inward: 'Inward', outward: 'Outward', rma: 'OEM RMA', employees: 'Employees',
  integrity: 'Data integrity', audit: 'Change log', users: 'Users and security', activity: 'Activity log' };

// Views are loaded on demand so the first screen stays small.
const VIEWS = {
  'dashboard/assets': () => import('./views/dash-assets.js').then((m) => m.mountAssetsDash),
  'dashboard/calls': () => import('./views/dash-calls.js').then((m) => m.mountCallsDash),
  'dashboard/engineers': () => import('./views/dash-engineers.js').then((m) => m.mountEngineersDash),
  integrity: () => import('./views/integrity.js').then((m) => m.mountIntegrity),
  audit: () => import('./views/audit.js').then((m) => m.mountAudit),
  pm: () => import('./views/dash-pm.js').then((m) => m.mountPmDash),
  'pm/cycles': () => import('./views/pm-cycles.js').then((m) => m.mountPmCycles),
  'pm/history': () => import('./views/pm-history.js').then((m) => m.mountPmHistory),
  reports: () => import('./views/reports.js').then((m) => m.mountReports),
  'admin/import': () => import('./views/admin-import.js').then((m) => m.mountImport),
  'admin/backup': () => import('./views/admin-backup.js').then((m) => m.mountBackup),
  'admin/update': () => import('./views/admin-update.js').then((m) => m.mountUpdate),
  'admin/email': () => import('./views/admin-email.js').then((m) => m.mountEmail),
  'admin/users': () => import('./views/admin-users.js').then((m) => m.mountUsers),
  'admin/activity': () => import('./views/admin-activity.js').then((m) => m.mountActivity),
};
const REGISTERS = ['assets', 'calls', 'inward', 'outward', 'rma', 'employees', 'engineers', 'pm'];

function skeleton() {
  app.className = 'shell';
  app.replaceChildren(h('header', { class: 'hdr', role: 'banner' }), h('nav', { class: 'nav', 'aria-label': 'Primary' }), h('main', { id: 'main', class: 'main', tabindex: '-1' }));
}

async function render(route) {
  if (!running) return;
  const main = $('#main');
  const key = route.path.slice(0, 2).join('/');
  const [a, b] = route.path;
  view?.destroy?.();
  destroyAll();
  closeDrawer();
  clear(main);
  main.scrollTop = 0;
  shell.setActive(route.path.join('/'));
  let mount = null;
  if (a === 'registers' && REGISTERS.includes(b)) mount = () => import('./views/register.js').then((m) => (root) => m.mountRegister(root, b));
  else if (VIEWS[key]) mount = () => VIEWS[key]().then((fn) => (root) => fn(root));
  else if (VIEWS[a]) mount = () => VIEWS[a]().then((fn) => (root) => fn(root));
  if (!mount) { view = null; router.go('dashboard/calls', null, { replace: true }); return; }
  main.append(h('div', { class: 'loading-line', role: 'status' }, 'Loading…'));
  let go;
  try { go = await mount(); } catch (e) { clear(main); main.append(h('div', { class: 'err' }, 'This view could not be loaded. ', e.message)); return; }
  if (!running || router.current().raw !== route.raw) return;
  clear(main);
  view = go(main);
  const kind = a === 'registers' ? 'Register' : a === 'dashboard' ? 'Dashboard' : 'Control';
  document.title = `${TITLES[b || a] || 'ITAM'} · ${kind} · ITAM Portal`;
  main.focus({ preventScroll: true });
  const p = route.path.join('/');
  if (p !== lastPath) { lastPath = p; send('/api/activity/view', { path: '#/' + p }).catch(() => {}); }
}

function startApp() {
  if (running) return;
  running = true;
  skeleton();
  shell = buildShell(app);
  live.start();
  session.start();
  render(router.current());
}

function stopApp() {
  if (!running) return;
  running = false;
  view?.destroy?.(); view = null;
  destroyAll(); closeDrawer(); closeAllModals();
  live.stop(); session.stop();
  lastPath = '';
}

let idleModal = null;
session.setHooks({
  warn(secondsLeft) {
    const t = h('strong', null, '');
    const tick = () => { t.textContent = `${secondsLeft()} s`; };
    tick();
    const iv = setInterval(tick, 1000);
    idleModal = openModal({ title: 'Still there?', locked: true, onClose: () => clearInterval(iv),
      body: h('p', null, 'You will be signed out soon because there has been no activity. Time left: ', t),
      actions: [{ label: 'Sign out now', onClick: () => session.signOut() }, { label: 'Stay signed in', primary: true, onClick: () => { session.markActive(true); } }] });
  },
  clearWarn() { idleModal?.close(); idleModal = null; },
});

let lastKey = '';
// Set the moment any part of the sign-in flow is shown (login form, 2FA, forced password change, forced 2FA setup); cleared by a page
// reload. When state next reaches 'ok', a true login just completed - a full reload is forced so the browser re-fetches index.html and
// whatever build it currently points to, rather than continuing to run whatever JS bundle happened to already be loaded in this tab
// (a hash-only route change never re-fetches anything, so a portal tab left open across a deploy would otherwise keep running stale code
// indefinitely). A session that was already valid when this tab opened never sets this flag, so it is not reloaded in a loop.
let freshLogin = false;
// True once this tab has been signed in. Signing out, an idle time-out or an expired session then reloads the whole page to a clean
// login - no route, drawer, cached data or script state from the previous session survives. The reason is carried across the reload
// in sessionStorage just to show one message on the login form.
let wasSignedIn = false;
const REASON_KEY = 'itam.reason';
const takeReason = () => { try { const r = sessionStorage.getItem(REASON_KEY); sessionStorage.removeItem(REASON_KEY); return r; } catch (_) { return null; } };
function gate() {
  const me = session.current();
  const key = `${me.authenticated}|${me.state}|${me.user?.username}`;
  if (key === lastKey && me.state === 'ok') return;
  lastKey = key;
  if (!me.authenticated) {
    if (wasSignedIn) {
      try { if (me.reason) sessionStorage.setItem(REASON_KEY, me.reason); } catch (_) { /* the message is optional */ }
      stopApp();
      history.replaceState(null, '', location.pathname);
      location.reload();
      return;
    }
    freshLogin = true;
    stopApp();
    app.className = 'login-root';
    const reason = me.reason || takeReason();
    mountLogin(app, { message: reason === 'idle' ? 'You were signed out because of inactivity.' : reason === 'expired' ? 'Your session ended. Please sign in again.' : null });
  } else if (me.state === '2fa') {
    freshLogin = true;
    stopApp();
    app.className = 'login-root';
    mountLogin(app, { message: 'Enter your verification code to finish signing in.', startAt2fa: true });
  } else if (me.state === 'change_password') {
    freshLogin = true;
    stopApp();
    app.className = 'login-root';
    mountLogin(app, {});
    passwordDialog({ forced: true });
  } else if (me.state === 'setup_2fa') {
    freshLogin = true;
    stopApp();
    app.className = 'login-root';
    mountLogin(app, {});
    twoFactorSetup({ forced: true });
  } else if (me.state === 'ok') {
    if (freshLogin) { history.replaceState(null, '', location.pathname + '#/dashboard/calls'); location.reload(); return; }   // every sign-in lands on the Call tracker
    wasSignedIn = true;
    closeAllModals();
    startApp();
  }
}

session.onSession(gate);
live.onAuth(() => session.signOut('expired'));
router.onRoute((route) => { if (running) render(route); });
live.onChange((ev) => view?.onLive?.(ev));
initHoverCards();
router.start();
session.load().catch(() => {
  app.className = 'login-root';
  app.replaceChildren(h('div', { class: 'login-page' }, h('div', { class: 'login-card' }, h('strong', null, 'The portal server is not reachable.'), h('p', { class: 'muted' }, 'Check that it is running, then reload this page.'))));
});
