// Sign-in screen and the account floating windows (first-login password change, two-factor set-up, account).
import { send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import * as session from '../core/session.js';
import { openModal } from '../ui/modal.js';
import { toast } from '../core/editor.js';

const FAIL_LIMIT = 3;           // wrong passwords before Sign in is swapped for Forgot password
const failKey = 'itam.loginFails';
const readFails = () => { try { return Number(sessionStorage.getItem(failKey)) || 0; } catch (_) { return 0; } };
const writeFails = (n) => { try { sessionStorage.setItem(failKey, String(n)); } catch (_) { /* private mode: the count just lives in memory */ } };

/** Full-page sign-in. Resolves when the person is fully signed in (password changed, second factor done). */
export function mountLogin(root, { message, startAt2fa = false } = {}) {
  root.replaceChildren();
  const user = h('input', { id: 'lg-user', type: 'text', autocomplete: 'username', autocapitalize: 'off', spellcheck: 'false', required: true, 'aria-describedby': 'lg-err' });
  const pass = h('input', { id: 'lg-pass', class: 'no-upper', type: 'password', autocomplete: 'current-password', required: true, 'aria-describedby': 'lg-err' });
  const eye = h('button', { class: 'btn ghost icon eye', type: 'button', 'aria-label': 'Show password', 'aria-pressed': 'false' }, icon('view'));
  eye.addEventListener('click', () => {
    const show = pass.type === 'password';
    pass.type = show ? 'text' : 'password';
    eye.setAttribute('aria-pressed', String(show));
    eye.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    eye.replaceChildren(icon(show ? 'view--off' : 'view'));
  });
  const code = h('input', { id: 'lg-code', type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '20', 'aria-describedby': 'lg-err', placeholder: '123456' });
  const err = h('div', { class: 'ferr', id: 'lg-err', role: 'alert', hidden: true });
  const go = h('button', { class: 'btn primary block', type: 'submit' }, 'Sign in');
  const step1 = h('div', null,
    h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'lg-user' }, 'User name'), user),
    h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'lg-pass' }, 'Password'), h('div', { class: 'pwbox' }, pass, eye)));
  const forgotBtn = h('button', { class: 'btn primary block', type: 'button', hidden: true }, icon('reset'), 'Forgot password');
  const forgotUser = h('input', { id: 'fp-user', type: 'text', autocomplete: 'username', autocapitalize: 'off', spellcheck: 'false' });
  const forgotMsg = h('div', { class: 'login-note', role: 'status', hidden: true });
  const forgotErr = h('div', { class: 'ferr', role: 'alert', hidden: true });
  const forgotSend = h('button', { class: 'btn primary block', type: 'submit' }, 'Send me a temporary password');
  const forgotBack = h('button', { class: 'btn ghost block', type: 'button' }, 'Back to sign in');
  const forgotPane = h('div', { hidden: true },
    h('p', { class: 'muted' }, 'Enter your user name. If your account has an e-mail address, a temporary password is sent there. If it has none, the request goes to the portal administrator, who will give you one.'),
    h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'fp-user' }, 'User name'), forgotUser), forgotMsg, forgotErr, forgotSend, forgotBack);
  const step2 = h('div', { hidden: true },
    h('p', { class: 'muted' }, 'Enter the 6-digit code from your authenticator app, or one of your recovery codes.'),
    h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'lg-code' }, 'Verification code'), code));
  const form = h('form', { class: 'login-card', novalidate: true, autocomplete: 'on' },
    h('img', { class: 'login-logo', src: '/static/assets/ongc-logo.png', alt: 'ONGC' }),
    h('div', { class: 'login-title' }, h('strong', null, 'ITAM PORTAL'), h('span', null, 'IT ASSET MANAGEMENT · ANKLESHWAR ASSET')),
    message ? h('div', { class: 'login-note', role: 'status' }, icon('information--filled'), message) : null,
    step1, step2, err, go, forgotBtn, forgotPane,
    h('div', { class: 'login-foot' }, 'Authorised users only. Activity is recorded.'));
  root.append(h('div', { class: 'login-page' }, form));
  let stage = 1;
  const showForgotButton = () => { const on = stage === 1 && readFails() >= FAIL_LIMIT; go.hidden = on; forgotBtn.hidden = !on; };
  const openForgot = () => {
    step1.hidden = true; go.hidden = true; forgotBtn.hidden = true; err.hidden = true; forgotPane.hidden = false;
    forgotUser.value = user.value.trim(); forgotMsg.hidden = true; forgotErr.hidden = true; forgotSend.hidden = false; forgotSend.disabled = false;
    forgotUser.focus();
  };
  forgotBtn.addEventListener('click', openForgot);
  forgotBack.addEventListener('click', () => {
    writeFails(0); forgotPane.hidden = true; step1.hidden = false; err.hidden = true; pass.value = ''; showForgotButton(); (user.value ? pass : user).focus();
  });
  forgotSend.addEventListener('click', async (e) => {
    e.preventDefault(); e.stopPropagation();
    forgotErr.hidden = true;
    if (!forgotUser.value.trim()) { forgotErr.textContent = 'Enter your user name.'; forgotErr.hidden = false; forgotUser.focus(); return; }
    forgotSend.disabled = true;
    try {
      const r = await send('/api/auth/forgot', { username: forgotUser.value.trim() });
      forgotMsg.replaceChildren(icon('information--filled'), r.message); forgotMsg.hidden = false; forgotSend.hidden = true; user.value = forgotUser.value.trim();
    } catch (ex) { forgotErr.textContent = ex.message; forgotErr.hidden = false; forgotSend.disabled = false; }
  });
  if (startAt2fa) { stage = 2; step1.hidden = true; step2.hidden = false; go.textContent = 'Verify'; setTimeout(() => code.focus(), 30); }

  showForgotButton();
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!forgotPane.hidden) { forgotSend.click(); return; }
    err.hidden = true;
    go.disabled = true;
    try {
      if (stage === 1) {
        if (!user.value.trim() || !pass.value) throw new Error('Enter your user name and password.');
        const me = await session.signIn(user.value.trim(), pass.value);
        writeFails(0);
        if (me.state === '2fa') { stage = 2; step1.hidden = true; step2.hidden = false; go.textContent = 'Verify'; code.focus(); }
      } else {
        await session.secondFactor(code.value);
      }
    } catch (ex) {
      err.textContent = ex.message; err.hidden = false; form.classList.remove('shake'); void form.offsetWidth; form.classList.add('shake');
      if (stage === 1 && ex.code === 'bad_credentials') writeFails(readFails() + 1);
      if (stage === 1 && ex.code === 'locked') writeFails(FAIL_LIMIT);
      showForgotButton();
      if (stage === 2 && ex.code === 'expired') { stage = 1; step1.hidden = false; step2.hidden = true; go.textContent = 'Sign in'; pass.value = ''; }
      (stage === 1 ? (forgotBtn.hidden ? pass : forgotBtn) : code).focus();
    } finally { go.disabled = false; }
  });
  if (!startAt2fa) setTimeout(() => user.focus(), 30);
}

// ---------------------------------------------------------------- password
const RULES = (min) => [
  ['len', `At least ${min} characters`, (p) => p.length >= min], ['lower', 'A lower-case letter', (p) => /[a-z]/.test(p)], ['upper', 'An upper-case letter', (p) => /[A-Z]/.test(p)],
  ['digit', 'A digit', (p) => /\d/.test(p)], ['sym', 'A symbol', (p) => /[^A-Za-z0-9]/.test(p)],
];

/** Floating window to change the password. `forced` = first sign-in / expired: cannot be dismissed. Resolves true once changed. */
export function passwordDialog({ forced = false } = {}) {
  return new Promise((resolve) => {
    const me = session.current();
    const cur = h('input', { id: 'pw-cur', class: 'no-upper', type: 'password', autocomplete: 'current-password' });
    const nw = h('input', { id: 'pw-new', class: 'no-upper', type: 'password', autocomplete: 'new-password' });
    const cf = h('input', { id: 'pw-cf', class: 'no-upper', type: 'password', autocomplete: 'new-password' });
    const list = h('ul', { class: 'rules', 'aria-label': 'Password rules' });
    const rules = RULES(me.policy_min || 12);
    const items = rules.map(([k, t]) => { const li = h('li', { 'data-k': k }, icon('close--outline'), t); list.append(li); return li; });
    const match = h('li', { 'data-k': 'match' }, icon('close--outline'), 'Both entries are the same');
    list.append(match);
    const paint = (li, ok) => { li.className = ok ? 'ok' : ''; li.replaceChildren(icon(ok ? 'checkmark--filled' : 'close--outline'), li.textContent); };
    const check = () => {
      rules.forEach(([, t, fn], i) => paint(items[i], fn(nw.value)));
      paint(match, nw.value !== '' && nw.value === cf.value);
    };
    nw.addEventListener('input', check); cf.addEventListener('input', check);
    const body = h('div', { class: 'no-upper' },
      h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'pw-cur' }, 'Current password'), cur),
      h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'pw-new' }, 'New password'), nw),
      h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'pw-cf' }, 'Repeat new password'), cf), list,
      h('p', { class: 'muted small' }, me.policy || ''));
    check();
    let done = false;
    openModal({
      title: forced ? 'Choose a new password' : 'Change password', locked: forced,
      lead: forced ? 'For security you must replace the temporary or expired password before you continue.' : null, body,
      onClose: () => resolve(done),
      actions: [forced ? null : { label: 'Cancel' }, { label: 'Change password', primary: true, keepOpen: true, onClick: async (m) => {
        if (nw.value !== cf.value) throw new Error('The two new passwords are not the same.');
        await session.changePassword(cur.value, nw.value);
        done = true; toast('Password changed'); m.close();
      } }].filter(Boolean),
    });
  });
}

// ---------------------------------------------------------------- two-factor
/** Set up an authenticator app. Resolves true when enabled. */
export async function twoFactorSetup({ forced = false } = {}) {
  const data = await send('/api/auth/2fa/begin', {});
  return new Promise((resolve) => {
    const qr = h('div', { class: 'qr', role: 'img', 'aria-label': 'QR code for the authenticator app' });
    qr.innerHTML = data.qr_svg;                       // SVG generated on the server by the QR library
    const code = h('input', { id: 'tf-code', type: 'text', inputmode: 'numeric', autocomplete: 'off', maxlength: '6', placeholder: '123456' });
    const body = h('div', { class: 'tf' },
      h('ol', { class: 'steps' },
        h('li', null, 'Install an authenticator app (Microsoft Authenticator, Google Authenticator, FreeOTP…).'),
        h('li', null, 'Scan this code, or enter the key by hand:'),
      ),
      qr, h('div', { class: 'secret mono' }, data.secret.replace(/(.{4})/g, '$1 ').trim()),
      h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'tf-code' }, 'Code shown in the app'), code));
    let ok = false;
    openModal({
      title: 'Two-factor sign-in', locked: forced, body, onClose: () => resolve(ok),
      actions: [forced ? null : { label: 'Cancel' }, { label: 'Turn on', primary: true, keepOpen: true, onClick: async (m) => {
        const r = await send('/api/auth/2fa/enable', { code: code.value });
        ok = true;
        await session.load();
        m.close();
        recoveryCodes(r.recovery_codes);
      } }].filter(Boolean),
    });
  });
}

function recoveryCodes(codes) {
  const text = codes.join('\n');
  const body = h('div', null, h('p', null, 'If you lose your phone you can sign in with one of these codes. Each works once. They are shown only now.'),
    h('pre', { class: 'codes mono' }, text));
  const ack = h('input', { type: 'checkbox', id: 'rc-ack' });
  body.append(h('label', { class: 'opt', for: 'rc-ack' }, ack, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, 'I have saved these codes')));
  openModal({ title: 'Recovery codes', locked: true, body, actions: [
    { label: 'Copy', icon: 'copy', keepOpen: true, onClick: async () => { try { await navigator.clipboard.writeText(text); toast('Copied'); } catch (_) { throw new Error('Copy is not available here. Select the codes and copy them.'); } return false; } },
    { label: 'Done', primary: true, keepOpen: true, onClick: (m) => { if (!ack.checked) throw new Error('Tick the box to confirm you have saved the codes.'); m.close(); } }] });
}

/** Account window: who you are, password, two-factor. */
export function accountDialog() {
  const me = session.current();
  const u = me.user;
  const tf = u.totp_enabled;
  const body = h('div', null,
    h('div', { class: 'kv' }, kv('User name', u.username), kv('Name', u.display_name), kv('Group', u.role === 'ADMIN' ? 'Administrator' : 'User'), kv('E-mail', u.email || '—'), kv('Two-factor sign-in', tf ? 'On' : 'Off')),
    h('p', { class: 'muted small' }, me.policy || ''));
  const acts = [{ label: 'Change password', keepOpen: true, onClick: async (m) => { m.close(); await passwordDialog(); return false; } }];
  if (!tf) acts.push({ label: 'Turn on two-factor', keepOpen: true, onClick: async (m) => { m.close(); await twoFactorSetup(); return false; } });
  else acts.push({ label: 'Turn off two-factor', keepOpen: true, onClick: (m) => { m.close(); disableTwoFactor(); return false; } });
  acts.push({ label: 'Sign out other sessions', keepOpen: true, onClick: async () => {
    try { const r = await send('/api/auth/logout-others', {}); toast(r.ended ? `Signed out ${r.ended} other session${r.ended === 1 ? '' : 's'}` : 'You are not signed in anywhere else'); } catch (e) { toast(e.message, 'bad'); }
    return false;
  } });
  acts.push({ label: 'Close', primary: true });
  openModal({ title: 'My account', body, actions: acts });
}

const kv = (k, v) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)];

function disableTwoFactor() {
  const pw = h('input', { id: 'td-pw', class: 'no-upper', type: 'password', autocomplete: 'current-password' });
  const code = h('input', { id: 'td-code', type: 'text', inputmode: 'numeric', autocomplete: 'off', maxlength: '6' });
  openModal({
    title: 'Turn off two-factor', lead: 'Enter your password and a current code to confirm.',
    body: h('div', null, h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'td-pw' }, 'Password'), pw), h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'td-code' }, 'Code'), code)),
    actions: [{ label: 'Cancel' }, { label: 'Turn off', danger: true, onClick: async () => { await send('/api/auth/2fa/disable', { password: pw.value, code: code.value }); await session.load(); toast('Two-factor turned off'); } }],
  });
}

