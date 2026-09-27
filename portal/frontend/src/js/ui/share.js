// Download / share menu used on every dashboard and report: Excel, PDF, CSV, or e-mail (with the file attached).
import { download, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../core/editor.js';
import { openModal } from './modal.js';

/** opts: {pack, formats:['pdf','xlsx'], endpoint, body: () => request body (reports)} -> element */
export function shareMenu({ pack, formats = ['pdf', 'xlsx'], endpoint = '/api/dashboard/export', body = () => ({ pack }), mail = true }) {
  const LABEL = { pdf: ['document', 'PDF document'], xlsx: ['data-table', 'Excel workbook'], csv: ['catalog', 'CSV (plain text)'] };
  const btn = h('button', { class: 'btn', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, icon('download'), 'Download / share', icon('chevron--down', 'sm'));
  const menu = h('div', { class: 'menu', role: 'menu', hidden: true });
  const wrap = h('div', { class: 'usr-wrap' }, btn, menu);
  const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  formats.forEach((f) => {
    menu.append(h('button', { role: 'menuitem', type: 'button', onClick: async () => {
      close();
      try { const name = await download(endpoint, { ...body(), format: f }); toast(`Downloaded ${name}`); } catch (e) { toast(e.message, 'bad'); }
    } }, icon(LABEL[f][0]), LABEL[f][1]));
  });
  if (mail) menu.append(h('button', { role: 'menuitem', type: 'button', onClick: () => { close(); mailDialog({ pack, body }); } }, icon('email'), 'Send by e-mail…'));
  btn.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; btn.setAttribute('aria-expanded', String(!menu.hidden)); if (!menu.hidden) menu.querySelector('button').focus(); });
  document.addEventListener('click', (e) => { if (!menu.hidden && !wrap.contains(e.target)) close(); });
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') { close(); btn.focus(); } });
  return wrap;
}

function mailDialog({ pack }) {
  const to = h('input', { id: 'sh-to', type: 'text', class: 'email', placeholder: 'name@company.com, other@company.com', autocomplete: 'off' });
  const msg = h('textarea', { id: 'sh-msg', rows: '3', maxlength: '1000', placeholder: 'Optional message' });
  const fmt = {};
  const box = (id, label, on) => {
    const i = h('input', { type: 'checkbox', id: 'sh-' + id }); i.checked = on; fmt[id] = i;
    return h('label', { class: 'opt', for: 'sh-' + id }, i, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, label(id)));
  };
  const label = (id) => (id === 'pdf' ? 'Attach PDF' : 'Attach Excel workbook');
  openModal({
    title: 'Send by e-mail', lead: 'The current figures are sent as attachments. Every share is recorded in the activity log.',
    body: h('div', null, h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'sh-to' }, 'To (separate with commas)'), to), h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'sh-msg' }, 'Message'), msg), box('pdf', '', true), box('xlsx', '', false)),
    actions: [{ label: 'Cancel' }, { label: 'Send', primary: true, icon: 'send', onClick: async () => {
      const list = to.value.split(/[;,\s]+/).filter(Boolean);
      const formats = Object.entries(fmt).filter(([, i]) => i.checked).map(([k]) => k);
      if (!formats.length) throw new Error('Choose at least one attachment.');
      const r = await send('/api/dashboard/share', { pack, to: list, formats, message: msg.value });
      toast(`Sent to ${r.sent_to.join(', ')}`);
    } }],
  });
}
