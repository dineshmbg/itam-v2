import { h, icon, clear } from '../core/dom.js';
import { clock } from '../core/format.js';
import { getTheme, setTheme } from '../core/theme.js';
import * as live from '../core/live.js';
import * as session from '../core/session.js';
import { createSearch } from './search.js';
import { accountDialog } from '../views/login.js';

// [path, label, icon, adminOnly, mod] - adminOnly: true = admins (and Users with extended access); 'strict' = real administrators only; 'lead' = admins, plus a Team Leader/SI User; 'lead_strict' = real administrators, plus a Team Leader/SI User;
// 'parts' = admins, plus a User individually granted
// Calls/Inward/Outward/OEM RMA access (session.hasCallPartsAccess()) - see Manage user -> Module access.
// mod: which --mod-* accent tints this item's icon (see tokens.css) - purely decorative wayfinding, never the only signal for anything.
const NAV = [
  ['Dashboards', [
    ['dashboard/assets', 'Assets', 'devices', false, 'assets'], ['dashboard/calls', 'Call tracker', 'phone', false, 'calls'], ['dashboard/engineers', 'Engineers', 'user--multiple', true, 'eng'],
  ]],
  ['Registers', [
    ['registers/assets', 'Assets', 'data-table', false, 'assets'], ['registers/calls', 'Calls', 'catalog', 'parts', 'calls'], ['registers/inward', 'Inward', 'package', 'parts', 'spr'],
    ['registers/outward', 'Outward', 'box', 'parts', 'spr'], ['registers/rma', 'OEM RMA', 'tools', 'parts', 'spr'],
  ]],
  ['People', [['registers/engineers', 'Engineers', 'user--avatar', false, 'eng']]],
  ['Preventive maintenance', [['pm', 'PM dashboard', 'analytics', false, 'pm'], ['pm/work', 'PM work orders', 'in-progress', false, 'pm'], ['pm/findings', 'PM findings', 'warning', false, 'pm'], ['registers/pm', 'PM worklist', 'checkmark--outline', false, 'pm'], ['pm/history', 'Past quarters', 'time', false, 'pm'], ['pm/cycles', 'Cycles and snapshots', 'calendar', 'lead', 'pm']]],
  ['Reports', [['reports', 'Report builder', 'report', false, 'rep'], ['reports/inventory', 'My inventory report', 'compare', false, 'rep']]],
  ['Control', [['integrity', 'Data integrity', 'security', 'strict', 'adm'], ['audit', 'Change log', 'catalog', 'strict', 'adm']]],
  ['Data tools', [['admin/import', 'Data import', 'upload', 'strict', 'data'], ['admin/match', 'Inventory match', 'compare', 'lead_strict', 'data'], ['admin/backup', 'Backup and restore', 'data--base', 'strict', 'data'], ['admin/update', 'Software update', 'restart', 'strict', 'data']]],
  ['Administration', [['admin/users', 'Users and security', 'user--multiple', 'lead_strict', 'adm'], ['admin/email', 'E-mail and alerts', 'email', 'strict', 'adm'], ['admin/activity', 'Activity log', 'time', 'lead_strict', 'adm']]],
];

const THEMES = [['light', 'Light', 'light'], ['dark', 'Dark', 'asleep'], ['auto', 'Automatic (follows system)', 'screen']];

export function buildShell(app) {
  const shell = app;
  const oldHdr = shell.querySelector('.hdr');
  const nav = shell.querySelector('.nav');
  if (localStorage.getItem('itam.nav') === 'min') shell.classList.add('nav-min');

  const menuBtn = h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Toggle navigation', 'aria-expanded': 'true' }, icon('menu', 'lg'));
  menuBtn.addEventListener('click', () => {
    if (window.matchMedia('(max-width: 900px)').matches) { shell.classList.toggle('nav-open'); return; }
    const min = shell.classList.toggle('nav-min');
    try { localStorage.setItem('itam.nav', min ? 'min' : 'max'); } catch (_) { /* ignore */ }
    menuBtn.setAttribute('aria-expanded', String(!min));
  });

  const search = createSearch();

  const liveDot = h('span', { class: 'live', role: 'status', 'data-state': live.getState() === 'on' ? 'on' : 'connecting' }, h('span', { class: 'dot' }), h('span', { class: 'lt' }, 'Connecting…'));
  const setLive = () => {
    const s = live.getState();
    liveDot.dataset.state = s;
    liveDot.querySelector('.lt').textContent = s === 'on' ? (live.getLast() ? `Live · updated ${clock(live.getLast())}` : 'Live') : s === 'off' ? 'Offline' : 'Reconnecting…';
  };
  live.onState(setLive);
  live.onChange(() => { setLive(); liveDot.classList.add('flash'); setTimeout(() => liveDot.classList.remove('flash'), 900); });
  setLive();

  const seg = h('div', { class: 'seg', role: 'radiogroup', 'aria-label': 'Colour theme' },
    THEMES.map(([v, label, ic]) => h('button', { type: 'button', role: 'radio', 'aria-checked': String(getTheme() === v), 'aria-label': label, title: label, 'data-theme-v': v }, icon(ic))));
  seg.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-theme-v]');
    if (!b) return;
    setTheme(b.dataset.themeV);
    seg.querySelectorAll('button').forEach((x) => x.setAttribute('aria-checked', String(x === b)));
  });
  seg.addEventListener('keydown', (e) => {
    const bs = [...seg.querySelectorAll('button')];
    const i = bs.indexOf(document.activeElement);
    if (i < 0 || !['ArrowRight', 'ArrowLeft'].includes(e.key)) return;
    e.preventDefault();
    const n = bs[(i + (e.key === 'ArrowRight' ? 1 : bs.length - 1)) % bs.length];
    n.focus(); n.click();
  });

  const helpBtn = h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Keyboard shortcuts', title: 'Keyboard shortcuts' }, icon('keyboard', 'lg'));
  helpBtn.addEventListener('click', showHelp);

  // signed-in user menu
  const me = session.user();
  const userBtn = h('button', { class: 'btn ghost usr', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false', title: 'Account' }, icon('user--avatar', 'lg'),
    h('span', { class: 'usr-t' }, h('span', { class: 'usr-n' }, me?.display_name || ''), h('span', { class: 'usr-r' }, (me?.role === 'ADMIN' ? 'Administrator' : me?.extended_access ? 'User · Extended access' : 'User') + (me?.read_only ? ' · Read-only' : ''))));
  const menu = h('div', { class: 'menu', role: 'menu', hidden: true },
    h('button', { role: 'menuitem', type: 'button', onClick: () => { closeMenu(); accountDialog(); } }, icon('user'), 'My account'),
    h('button', { role: 'menuitem', type: 'button', onClick: () => { closeMenu(); session.signOut(); } }, icon('logout'), 'Sign out'));
  const closeMenu = () => { menu.hidden = true; userBtn.setAttribute('aria-expanded', 'false'); };
  userBtn.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; userBtn.setAttribute('aria-expanded', String(!menu.hidden)); if (!menu.hidden) menu.querySelector('button').focus(); });
  document.addEventListener('click', (e) => { if (!menu.hidden && !menu.contains(e.target)) closeMenu(); });
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeMenu(); userBtn.focus(); } });

  const hdr = h('header', { class: 'hdr', role: 'banner' },
    menuBtn,
    h('a', { class: 'brand', href: '#/dashboard/calls', 'aria-label': 'ITAM Portal home' }, h('span', { class: 'brand-mark' }),
      h('span', { class: 'brand-text' }, h('span', { class: 'brand-line' }, h('strong', null, 'ITAM'), h('span', { class: 'brand-sub' }, 'Portal')), h('span', { class: 'brand-sig' }, 'Dinesh Gadaria'))),
    search.el, h('span', { class: 'hdr-spacer' }),
    h('div', { class: 'hdr-tools' }, liveDot, seg, helpBtn, h('div', { class: 'usr-wrap' }, userBtn, menu)));
  oldHdr.replaceWith(hdr);

  clear(nav);
  NAV.forEach(([title, items]) => {
    const shown = items.filter(([, , , adminOnly]) => !adminOnly || (adminOnly === 'strict' || adminOnly === 'lead_strict' ? session.isFullAdmin() : session.isAdmin()) || (adminOnly.startsWith?.('lead') && session.hasLeadTools()) || (adminOnly === 'parts' && session.hasCallPartsAccess()));
    if (!shown.length) return;
    nav.append(h('div', { class: 'nav-group' }, h('h2', null, title), shown.map(([path, label, ic, , mod]) =>
      h('a', { class: 'nav-item', href: '#/' + path, 'data-path': path, title: label, style: mod ? { '--mod-c': `var(--mod-${mod})` } : null }, h('span', { class: 'ico-wrap' }, icon(ic, 'lg')), h('span', null, label)))));
  });
  nav.append(h('div', { class: 'nav-foot' }, h('div', null, 'Live view of the IT asset inventory.')));

  if (!shell.querySelector('.app-foot')) {
    const sig = h('span', { class: 'af-sig', 'aria-hidden': 'true' }, 'Dinesh Gadaria');
    shell.append(h('footer', { class: 'app-foot', role: 'contentinfo' },
      h('span', { class: 'af-l' }, h('span', { class: 'af-dot' }), 'ITAM Portal', h('span', { class: 'af-sep' }, '·'), 'IT asset management · Ankleshwar Asset'),
      h('span', { class: 'af-mid' }, sig),
      h('span', { class: 'af-r' }, 'Authorised users only · activity is recorded')));
    scheduleSignatureSweep(sig);
  }

  nav.addEventListener('click', () => shell.classList.remove('nav-open'));
  return {
    setActive(path) {
      if (path.startsWith('pm/wo/')) path = 'pm/work';      // a work order opened from the list keeps that list highlighted
      const items = [...nav.querySelectorAll('a.nav-item')];
      const hit = items.filter((a) => path === a.dataset.path || path.startsWith(a.dataset.path + '/')).sort((x, y) => y.dataset.path.length - x.dataset.path.length)[0];
      items.forEach((a) => { if (a === hit) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
    },
  };
}

// Footer signature: fades in, centred in the middle of the footer, in a random chart colour, holds for 10 seconds, fades out -
// then nothing until the next pass roughly 10 minutes later. Purely decorative - nothing else in the portal reads or depends
// on this element.
const SIG_INTERVAL_MS = 10 * 60 * 1000;
const SIG_SHOW_MS = 10_000;
function scheduleSignatureSweep(el) {
  function sweep() {
    if (!el.isConnected) return;                                    // the shell is torn down on sign-out
    el.style.color = `var(--chart-${1 + Math.floor(Math.random() * 10)})`;
    el.classList.add('run');
    setTimeout(() => { el.classList.remove('run'); setTimeout(sweep, SIG_INTERVAL_MS); }, SIG_SHOW_MS);
  }
  setTimeout(sweep, 10_000);                                         // first pass soon after sign-in, then every 10 minutes
}

function showHelp() {
  import('./drawer.js').then(({ openDrawer }) => {
    const d = openDrawer({ title: 'Keyboard shortcuts', subtitle: 'Everything is reachable without a mouse.' });
    const rows = [['/  or  Ctrl + K', 'Search everything'], ['↑ ↓', 'Move through search results or table rows'], ['Enter', 'Open the selected result / row'],
      ['Esc', 'Close search, details or a floating window'], ['Tab', 'Move between filters, table and controls'], ['Space', 'Toggle a focused filter option']];
    d.body.append(h('div', { class: 'dsec' }, h('div', { class: 'help-grid' }, rows.flatMap(([k, t]) => [h('kbd', null, k), h('span', null, t)]))));
  });
}
