// User management (administrators): accounts, groups, password reset, unlock, two-factor reset and the security policy. Everything happens in floating windows.
import { get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { person } from '../core/format.js';
import { toast } from '../core/editor.js';
import * as session from '../core/session.js';
import { confirmBox, openModal } from '../ui/modal.js';
import { errorBlock, pageHead } from './common.js';
import { when } from './audit.js';

const SETTING_FIELDS = [
  ['pw_min_length', 'Minimum password length', 'characters', 8, 64], ['pw_history', 'Passwords remembered (cannot be reused)', 'passwords', 0, 24],
  ['pw_max_age_days', 'Password expires after (0 = never)', 'days', 0, 730], ['lockout_attempts', 'Failed sign-ins before lock-out', 'attempts', 3, 20],
  ['lockout_minutes', 'Lock-out duration', 'minutes', 1, 1440], ['idle_timeout_min', 'Sign out after inactivity', 'minutes', 1, 480], ['session_max_hours', 'Longest session', 'hours', 1, 72],
];

export function mountUsers(root) {
  let dead = false, data = null;
  const holder = h('div', { class: 'page' });
  root.append(holder);
  const body = h('div', { class: 'panel-b flush' });

  const addBtn = h('button', { class: 'btn', type: 'button', onClick: newUserDialog }, icon('add'), 'Add user');
  const syncBtn = h('button', { class: 'btn primary', type: 'button', onClick: syncRoster }, icon('renew'), 'Sync from roster');
  const setBtn = h('button', { class: 'btn', type: 'button', onClick: securityDialog }, icon('security'), 'Security settings');
  holder.append(pageHead('Users and security', 'Two groups: administrators manage everything; users work with the registers within their permissions. Sync from roster covers everyone on the CIPL roster; Add user covers anyone else.', [setBtn, addBtn, syncBtn]),
    h('section', { class: 'panel' }, body));

  function newUserDialog() {
    const f = (id, label, el, hint) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), el, hint ? h('div', { class: 'hint' }, hint) : null);
    const username = h('input', { id: 'nu-name', type: 'text', maxlength: '30', autocomplete: 'off' });
    const name = h('input', { id: 'nu-full', type: 'text', maxlength: '60', autocomplete: 'off' });
    const email = h('input', { id: 'nu-mail', type: 'email', class: 'email', maxlength: '120', autocomplete: 'off' });
    const role = h('div', { class: 'rgroup', role: 'radiogroup', 'aria-label': 'Group' }, [['USER', 'User'], ['ADMIN', 'Administrator']].map(([v, t], i) => {
      const r = h('input', { type: 'radio', name: 'nu-role', value: v }); r.checked = i === 0;
      return h('label', { class: 'ropt' }, r, icon('radio-button', 'glyph off'), icon('radio-button--checked', 'glyph on'), h('span', null, t));
    }));
    const eng = h('select', { id: 'nu-eng' }, [h('option', { value: '' }, '— none —'), ...data.engineers.map((k) => h('option', { value: k }, person(k)))]);
    openModal({
      title: 'Add user', lead: 'For anyone not on the CIPL roster - a general ONGC employee, say. They get a temporary password and must choose a real one at first sign-in.',
      body: h('div', null,
        f('nu-name', 'User name', username, 'e.g. their CPF number. Roster accounts use the ECODE, so pick something else to avoid a clash.'),
        f('nu-full', 'Full name', name), f('nu-mail', 'E-mail (for notifications, optional)', email),
        h('div', { class: 'frow' }, h('span', { class: 'flabel' }, 'Group'), role), f('nu-eng', 'Linked engineer (optional)', eng)),
      actions: [{ label: 'Cancel' }, { label: 'Add user', primary: true, onClick: async () => {
        const r = await send('/api/admin/users/create', { username: username.value, display_name: name.value, email: email.value, role: role.querySelector('input:checked').value, engineer_key: eng.value });
        data.users = r.users; draw();
        tempPassword(r.username, r.temporary_password);
      } }],
    });
  }

  async function syncRoster() {
    syncBtn.disabled = true;
    try {
      const r = await send('/api/admin/users/sync', {});
      data.users = r.users; draw();
      syncResult(r);
    } catch (e) { toast(e.message, 'bad'); } finally { syncBtn.disabled = false; }
  }

  function syncResult(r) {
    const list = (items, empty) => (items.length ? h('ul', { class: 'sync-list' }, items.map((x) => h('li', null, x))) : h('p', { class: 'muted small' }, empty));
    openModal({
      title: 'Synced from roster', lead: 'One account per active CIPL employee. User name = ECODE. Site in-charge / SI and Sr Server Engineer are administrators; the rest are users. No login for Office Boy.',
      wide: true,
      body: h('div', null,
        h('div', { class: 'dsec' }, h('h3', null, `Created (${r.created.length})`), h('p', { class: 'muted small' }, r.created.length ? 'Password for each is the same as their user name (the ECODE). They must change it at first sign-in.' : ''), list(r.created, 'No new employees to add.')),
        h('div', { class: 'dsec' }, h('h3', null, `Updated (${r.updated.length})`), list(r.updated, 'Nothing needed updating.')),
        h('div', { class: 'dsec' }, h('h3', null, `No login (Office Boy) (${r.skipped_no_login.length})`), list(r.skipped_no_login, 'None.')),
        r.bad_email?.length ? h('div', { class: 'dsec' }, h('h3', null, `E-mail not valid in the roster (${r.bad_email.length})`), h('p', { class: 'muted small' }, 'The account was still created; fix the address in the CIPL roster sheet and sync again.'), list(r.bad_email, '')) : null,
        r.errors.length ? h('div', { class: 'dsec' }, h('h3', null, `Could not add (${r.errors.length})`), h('ul', { class: 'sync-list' }, r.errors.map((e) => h('li', null, `${e.ecode}: ${e.error}`)))) : null),
      actions: [{ label: 'Done', primary: true }],
    });
  }

  async function load() {
    try {
      data = await get('/api/admin/users');
      if (dead) return;
      draw();
    } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, load)); }
  }

  function badgeFor(u) {
    if (!u.active) return h('span', { class: 'badge mute' }, icon('close--outline'), 'Deactivated');
    if (u.locked) return h('span', { class: 'badge bad' }, icon('locked'), 'Locked');
    if (u.must_change) return h('span', { class: 'badge warn' }, icon('time'), 'Must change password');
    return h('span', { class: 'badge ok' }, icon('checkmark--filled'), 'Active');
  }

  function draw() {
    const me = session.user();
    // accounts nobody has ever signed in to are still on their starting password - the most guessable thing in the system
    const idle = data.users.filter((u) => u.active && !u.read_only && !u.last_login_at);
    const notice = idle.length ? h('div', { class: 'rec-note warn', style: { margin: '12px' } }, icon('warning'),
      `${idle.length} active account${idle.length === 1 ? ' has' : 's have'} never signed in and still ${idle.length === 1 ? 'uses' : 'use'} the starting password: ${idle.map((u) => u.username).join(', ')}. `
      + 'Ask each person to sign in and choose a password, or use Manage > Reset password for a one-time password, or deactivate accounts that are not needed.') : null;
    body.replaceChildren(...(notice ? [notice] : []), h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['User name', 'Name', 'Group', 'Status', 'Two-factor', 'E-mail', 'Last sign-in', ''].map((t) => h('th', null, t)))),
      h('tbody', null, data.users.map((u) => h('tr', null,
        h('td', { class: 'mono' }, u.username, u.user_id === me.user_id ? h('span', { class: 'faint' }, ' (you)') : null), h('td', null, person(u.display_name)),
        h('td', null, h('span', { class: 'badge ' + (u.role === 'ADMIN' ? 'info' : 'mute') }, u.role === 'ADMIN' ? 'Administrator' : 'User'),
          u.role !== 'ADMIN' && u.extended_access ? h('span', { class: 'badge info', style: { marginLeft: '4px' } }, 'Extended access') : null,
          u.role !== 'ADMIN' && !u.extended_access && u.asset_access && u.asset_access !== 'NONE' ? h('span', { class: 'badge info', style: { marginLeft: '4px' } }, 'Asset ' + u.asset_access.toLowerCase()) : null,
          u.read_only ? h('span', { class: 'badge mute', style: { marginLeft: '4px' } }, 'Read-only') : null), h('td', null, badgeFor(u)),
        h('td', null, u.totp_enabled ? h('span', { class: 'tick' }, icon('checkmark--filled'), ' On') : h('span', { class: 'faint' }, icon('close--outline'), ' Off')),
        h('td', null, u.email ? h('span', { class: 'email' }, u.email) : h('span', { class: 'faint' }, '—')), h('td', { class: 'nowrap' }, u.last_login_at ? when(u.last_login_at) : h('span', { class: 'faint' }, 'never')),
        h('td', { class: 'right' }, h('button', { class: 'btn', type: 'button', onClick: () => userDialog(u) }, icon('edit'), 'Manage'))))))));
  }

  function userDialog(u) {
    const f = (id, label, el, hint) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), el, hint ? h('div', { class: 'hint' }, hint) : null);
    const username = h('input', { id: 'u-name', type: 'text', maxlength: '30', value: u.username, disabled: true, autocomplete: 'off' });
    const name = h('input', { id: 'u-full', type: 'text', maxlength: '60', value: u.display_name, autocomplete: 'off' });
    const email = h('input', { id: 'u-mail', type: 'email', class: 'email', maxlength: '120', value: u.email || '', autocomplete: 'off' });
    const role = h('div', { class: 'rgroup', role: 'radiogroup', 'aria-label': 'Group' }, [['USER', 'User'], ['ADMIN', 'Administrator']].map(([v, t]) => {
      const i = h('input', { type: 'radio', name: 'u-role', value: v }); i.checked = u.role === v;
      return h('label', { class: 'ropt' }, i, icon('radio-button', 'glyph off'), icon('radio-button--checked', 'glyph on'), h('span', null, t));
    }));
    const eng = h('select', { id: 'u-eng' }, [h('option', { value: '' }, '— none —'), ...data.engineers.map((k) => h('option', { value: k }, person(k)))]);
    eng.value = u.engineer_key || '';
    const parts = h('select', { id: 'u-parts' }, [['NONE', 'No access'], ['READ', 'Read-only'], ['FULL', 'Full access (view, add, edit)']].map(([v, t]) => h('option', { value: v }, t)));
    parts.value = u.call_parts_access || 'NONE';
    const assetAccess = h('select', { id: 'u-assets' }, [['NONE', 'No extra access - their own assigned assets only'], ['READ', 'View the whole fleet (dashboard, register, reports) - no editing'], ['FULL', 'View the whole fleet, and edit / add / archive any asset']].map(([v, t]) => h('option', { value: v }, t)));
    assetAccess.value = u.asset_access || 'NONE';
    const ext = h('input', { type: 'checkbox', id: 'u-ext' }); ext.checked = !!u.extended_access;
    const extRow = h('label', { class: 'opt', for: 'u-ext' }, ext, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'),
      h('span', { class: 'name' }, 'Extended access - administrator-level access to dashboards, registers, people, preventive maintenance and reports'));
    const extHint = h('div', { class: 'hint' }, 'For a User account only: everything unscoped, but no Control, Data tools or Administration.');
    const extWrap = h('div', { class: 'frow' }, extRow, extHint);
    const syncExt = () => { extWrap.hidden = role.querySelector('input:checked')?.value !== 'USER'; };
    role.addEventListener('change', syncExt); syncExt();
    const active = h('input', { type: 'checkbox', id: 'u-active' }); active.checked = u.active;
    const activeRow = h('label', { class: 'opt', for: 'u-active' }, active, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, 'Account is active (can sign in)'));
    const extra = h('div', { class: 'btn-row' },
      h('button', { class: 'btn', type: 'button', onClick: () => resetPassword(u) }, icon('reset'), 'Reset password'),
      u.locked ? h('button', { class: 'btn', type: 'button', onClick: () => act(u, { unlock: true }, 'Account unlocked') }, icon('unlocked'), 'Unlock') : null,
      u.totp_enabled ? h('button', { class: 'btn', type: 'button', onClick: () => confirmBox({ title: 'Reset two-factor sign-in', lead: `${u.username} will have to set up an authenticator app again.`, confirm: 'Reset' }).then((ok) => ok && act(u, { reset_2fa: true }, 'Two-factor reset')) }, icon('renew'), 'Reset two-factor') : null);
    openModal({
      title: `Manage ${u.username}`, lead: u.read_only ? 'This is a read-only account - it cannot sign in with elevated access, and nothing it does changes data.' : null,
      body: h('div', null, f('u-name', 'User name', username), f('u-full', 'Full name', name), f('u-mail', 'E-mail (for notifications)', email),
        h('div', { class: 'frow' }, h('span', { class: 'flabel' }, 'Group'), role), f('u-eng', 'Linked engineer (optional)', eng),
        f('u-parts', 'Calls / Inward / Outward / OEM RMA access', parts, 'For a User account only - an administrator already has full access to every register.'),
        f('u-assets', 'Asset dashboard / register / reports access', assetAccess, 'Independent of Extended access below - never grants Engineers, PM, or editing beyond Assets on its own.'), extWrap,
        u.role_locked ? h('p', { class: 'hint' }, 'Group set here by hand - Sync from roster keeps it.') : null, activeRow, extra),
      actions: [{ label: 'Cancel' }, { label: 'Save', primary: true, onClick: async () => {
        const g = role.querySelector('input:checked').value;
        await send('/api/admin/users/update', { user_id: u.user_id, display_name: name.value, email: email.value, role: g, active: active.checked, engineer_key: eng.value, call_parts_access: parts.value, asset_access: assetAccess.value, extended_access: g === 'USER' && ext.checked });
        toast('User saved'); await load();
      } }],
    });
  }

  async function act(u, changes, msg) {
    try { await send('/api/admin/users/update', { user_id: u.user_id, ...changes }); toast(msg); await load(); } catch (e) { toast(e.message, 'bad'); }
  }

  function resetPassword(u) {
    // Two ways out: show the temporary password here, or send it straight to the user's own address (only offered when there is one).
    const reset = (email) => async (m) => {
      const r = await send('/api/admin/users/reset-password', { user_id: u.user_id, email });
      m.close(); await load();
      if (email) toast(`Temporary password sent to ${r.emailed_to}`);
      else tempPassword(r.username, r.temporary_password);
    };
    openModal({
      title: 'Reset password',
      lead: `${u.username} will be signed out everywhere and must choose a new password at the next sign-in.`,
      body: h('p', { class: 'muted' }, u.email ? `E-mail sends the temporary password to ${u.email}. If it cannot be sent, nothing is changed.`
        : 'This account has no e-mail address, so the temporary password can only be shown here. Add an address under Manage to e-mail it instead.'),
      actions: [{ label: 'Cancel' },
        { label: 'Reset and show', danger: !u.email, onClick: reset(false) },
        ...(u.email ? [{ label: 'Reset and e-mail', icon: 'email', danger: true, onClick: reset(true) }] : [])],
    });
  }

  function tempPassword(username, pw) {
    openModal({ title: 'Temporary password', locked: true, lead: `Give this to ${username}. It is shown only now and must be changed at first sign-in.`,
      body: h('div', { class: 'secret mono big' }, pw),
      actions: [{ label: 'Copy', icon: 'copy', keepOpen: true, onClick: async () => { try { await navigator.clipboard.writeText(pw); toast('Copied'); } catch (_) { toast('Copy is not available here. Select the text and copy it.'); } return false; } }, { label: 'Done', primary: true }] });
  }

  function securityDialog() {
    const s = data.settings;
    const inputs = SETTING_FIELDS.map(([k, label, unit, lo, hi]) => {
      const i = h('input', { id: 's-' + k, type: 'number', min: String(lo), max: String(hi), value: String(s[k]), inputmode: 'numeric' });
      return [k, h('div', { class: 'frow row-inline' }, h('label', { class: 'flabel', for: 's-' + k }, label), h('div', { class: 'unit' }, i, h('span', { class: 'muted' }, unit))), i];
    });
    const tfa = h('input', { type: 'checkbox', id: 's-2fa' }); tfa.checked = !!s.require_2fa_admin;
    openModal({
      title: 'Security settings', lead: 'Applies to everyone. Password rules are checked whenever a password is set or changed.',
      body: h('div', null, inputs.map((x) => x[1]), h('label', { class: 'opt', for: 's-2fa' }, tfa, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, 'Administrators must use two-factor sign-in'))),
      actions: [{ label: 'Cancel' }, { label: 'Save settings', primary: true, onClick: async () => {
        const settings = Object.fromEntries(inputs.map(([k, , i]) => [k, i.value]));
        settings.require_2fa_admin = tfa.checked;
        await send('/api/admin/settings', { settings });
        toast('Security settings saved'); await load();
      } }],
    });
  }

  load();
  return { destroy() { dead = true; }, onLive: () => {} };
}
