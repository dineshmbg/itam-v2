// E-mail set-up and the automatic notification rules. Nothing is sent until e-mail is switched on and a rule is enabled.
import { get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../core/editor.js';
import { openModal } from '../ui/modal.js';
import { errorBlock, pageHead, panel } from './common.js';
import { when } from './audit.js';

const PARAM = { lookback_days: 'Look back (days)', within_days: 'Cover ends within (days)', older_than_days: 'Older than (days)' };
const STATUS = { SENT: ['ok', 'Sent'], FAILED: ['bad', 'Failed'], SKIPPED: ['mute', 'Skipped'] };

export function mountEmail(root) {
  let dead = false, data = null;
  const holder = h('div', { class: 'page' });
  root.append(holder);
  const body = h('div', { class: 'grid' });
  holder.append(pageHead('E-mail and notifications', 'Set up the mail server once, then choose which events e-mail the engineer concerned.'), body);

  async function load() {
    try { data = await get('/api/admin/email'); if (dead) return; draw(); } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, load)); }
  }

  function smtpPanel() {
    const s = data.smtp;
    const inp = (id, label, props = {}, hint) => { const i = h('input', { id, type: 'text', autocomplete: 'off', ...props }); return [h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), i, hint ? h('div', { class: 'hint' }, hint) : null), i]; };
    const [hostRow, host] = inp('em-host', 'Mail server', { value: s.host, placeholder: 'smtp.company.local', class: 'email' });
    const [portRow, port] = inp('em-port', 'Port', { value: String(s.port), inputmode: 'numeric' });
    const sec = h('select', { id: 'em-sec' }, [['none', 'None (plain, internal relay)'], ['starttls', 'STARTTLS'], ['ssl', 'SSL / TLS']].map(([v, t]) => h('option', { value: v }, t))); sec.value = s.security;
    const [userRow, user] = inp('em-user', 'User name (if the server needs one)', { value: s.user, class: 'email' });
    const pw = h('input', { id: 'em-pw', type: 'password', autocomplete: 'new-password', placeholder: s.has_password ? '•••••••• (saved - leave empty to keep)' : '' });
    const [fromRow, from] = inp('em-from', 'Sender address', { value: s.from_addr, placeholder: 'itam-portal@company.com', class: 'email' });
    const [nameRow, name] = inp('em-name', 'Sender name', { value: s.from_name });
    const [urlRow, url] = inp('em-url', 'Portal address (shown in e-mails)', { value: s.portal_url, placeholder: 'http://server:8420', class: 'email' });
    const on = h('input', { type: 'checkbox', id: 'em-on' }); on.checked = s.enabled;
    const testTo = h('input', { id: 'em-to', type: 'text', class: 'email', placeholder: 'your@address' });
    const saveBtn = h('button', { class: 'btn primary', type: 'button', onClick: async () => {
      try {
        const r = await send('/api/admin/email/save', { smtp: { enabled: on.checked, host: host.value, port: port.value, security: sec.value, user: user.value, from_addr: from.value, from_name: name.value, portal_url: url.value }, password: pw.value });
        data.smtp = r.smtp; toast('E-mail settings saved'); load();
      } catch (e) { toast(e.message, 'bad'); }
    } }, icon('save'), 'Save');
    const testBtn = h('button', { class: 'btn', type: 'button', onClick: async () => {
      try { const r = await send('/api/admin/email/test', { to: testTo.value }); toast(`Test message sent to ${r.sent_to}`); } catch (e) { toast(e.message, 'bad'); }
    } }, icon('send'), 'Send test');
    return panel('Mail server', { cls: 'span-5' }, h('div', null,
      h('label', { class: 'opt', for: 'em-on' }, on, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, 'E-mail is switched on (notifications and sharing can send)')),
      hostRow, portRow, h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'em-sec' }, 'Connection security'), sec), userRow,
      h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'em-pw' }, 'Password'), pw, h('div', { class: 'hint' }, 'Kept in a protected file on this server, not in the database or the browser.')),
      fromRow, nameRow, urlRow, h('div', { class: 'btn-row' }, saveBtn), h('hr'), h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'em-to' }, 'Send a test message to'), testTo), h('div', { class: 'btn-row' }, testBtn)));
  }

  function rulesPanel() {
    return panel('Automatic notifications', { cls: 'span-7', flush: true, hint: 'each message goes to the engineer concerned' }, h('div', { class: 'rules-list' }, data.rules.map((r) => {
      const on = h('input', { type: 'checkbox', id: 'ru-' + r.key }); on.checked = r.enabled;
      const extra = h('input', { type: 'text', class: 'email', placeholder: 'Also send to (optional): lead@company.com', value: r.extra_to || '', 'aria-label': 'Also send to' });
      const params = Object.keys(r.default_params).map((k) => { const i = h('input', { type: 'number', min: '1', max: '365', value: String(r.params[k]), 'aria-label': PARAM[k] }); return [k, i, h('label', { class: 'pr' }, PARAM[k], i)]; });
      const save = async () => {
        try { await send('/api/admin/email/rule', { key: r.key, enabled: on.checked, extra_to: extra.value, params: Object.fromEntries(params.map(([k, i]) => [k, i.value])) }); toast(`${r.label}: ${on.checked ? 'on' : 'off'}`); } catch (e) { toast(e.message, 'bad'); on.checked = r.enabled; }
      };
      on.addEventListener('change', save); extra.addEventListener('change', save); params.forEach(([, i]) => i.addEventListener('change', save));
      return h('div', { class: 'rule' },
        h('label', { class: 'opt', for: 'ru-' + r.key }, on, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, h('strong', null, r.label), h('div', { class: 'muted small' }, r.description))),
        h('div', { class: 'rule-x' }, params.map((p) => p[2]), extra, h('button', { class: 'btn ghost', type: 'button', onClick: () => preview(r) }, icon('view'), 'Preview')));
    })));
  }

  async function preview(r) {
    try {
      const res = await send('/api/admin/email/run', { dry_run: true, only: r.key });
      openModal({ title: `Preview · ${r.label}`, lead: 'What would be sent right now. Nothing is sent by a preview.', wide: true,
        body: res.items.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['Engineer', 'To', 'Subject', 'Rows'].map((t) => h('th', null, t)))),
          h('tbody', null, res.items.map((i) => h('tr', null, h('td', null, i.engineer), h('td', { class: 'wrap' }, i.recipients.length ? h('span', { class: 'email' }, i.recipients.join(', ')) : h('span', { class: 'badge bad' }, icon('warning--alt--filled'), 'no e-mail address on file')), h('td', { class: 'wrap' }, i.subject), h('td', null, String(i.count))))))) : h('p', { class: 'muted' }, 'Nothing to send right now.'),
        actions: [{ label: 'Close', primary: true }] });
    } catch (e) { toast(e.message, 'bad'); }
  }

  function logPanel() {
    return panel('Recent messages', { cls: 'span-12', flush: true }, data.log.rows.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['When', 'Rule', 'To', 'Subject', 'Result'].map((t) => h('th', null, t)))),
      h('tbody', null, data.log.rows.map((r) => h('tr', null, h('td', { class: 'nowrap' }, when(r.at)), h('td', null, r.rule_key.replace(/_/g, ' ').toLowerCase()), h('td', null, h('span', { class: 'email' }, r.recipient)), h('td', { class: 'wrap' }, r.subject),
        h('td', { class: 'wrap' }, h('span', { class: 'badge ' + STATUS[r.status][0] }, STATUS[r.status][1]), r.error ? h('div', { class: 'faint small' }, r.error) : null)))))) : h('div', { class: 'muted', style: { padding: '16px' } }, 'Nothing has been sent yet.'));
  }

  function draw() { body.replaceChildren(smtpPanel(), rulesPanel(), logPanel()); }

  load();
  return { destroy() { dead = true; }, onLive: () => {} };
}
