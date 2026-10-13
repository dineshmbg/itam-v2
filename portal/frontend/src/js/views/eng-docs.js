// Engineer documents (cancelled cheque, medical card, ESIC card, gate pass, other): the "Documents" tab of an engineer's record.
// An administrator, a Team Leader/SI and the engineer themselves may add a file; only an administrator / Team Leader/SI may remove one.
import { downloadGet, get, send, upload } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { bytes, date, when } from '../core/format.js';
import { toast } from '../ui/toast.js';
import { openModal } from '../ui/modal.js';

const STATUS = { EXPIRED: ['bad', 'Expired'], SOON: ['warn', 'Expires soon'] };

/** @returns the tab's element; it loads itself. */
export function engineerDocs(key) {
  const root = h('div', { class: 'dsec edocs' }, h('p', { class: 'muted' }, 'Loading…'));
  const base = `/api/engineers/${encodeURIComponent(key)}/docs`;
  let st = null;

  async function load() {
    try { st = await get(base); } catch (e) { root.replaceChildren(h('div', { class: 'rec-note bad', role: 'alert' }, icon('error--filled'), e.message)); return; }
    draw();
  }

  const fileLink = (d) => h('button', { class: 'link-btn', type: 'button', title: 'Download', onClick: () => downloadGet(`${base}/${d.doc_id}/file`, null, d.filename).catch((e) => toast(e.message, 'bad')) }, d.filename);

  function row(type, label, d) {
    const [tone, text] = d ? (STATUS[d.expiry_status] || ['ok', 'On file']) : ['mute', 'Missing'];
    const info = d ? h('div', { class: 'edoc-info' }, fileLink(d), h('span', { class: 'faint' }, ` · ${bytes(d.size_bytes)} · added by ${d.uploaded_by}, ${when(d.uploaded_at)}`),
      d.expiry_date ? h('div', { class: 'faint' }, `Valid until ${date(d.expiry_date)}`) : null) : h('div', { class: 'edoc-info faint' }, 'Nothing uploaded yet');
    return h('li', { class: 'edoc' }, h('div', { class: 'edoc-h' }, h('strong', null, label), h('span', { class: 'badge ' + tone }, text)), info,
      h('div', { class: 'edoc-act' },
        h('button', { class: 'btn', type: 'button', onClick: () => addDialog(type, d?.label) }, icon('upload'), d ? 'Replace' : 'Upload'),
        d && st.can_remove ? h('button', { class: 'btn ghost', type: 'button', onClick: () => removeDialog(d) }, icon('trash-can'), 'Remove') : null));
  }

  function draw() {
    const cur = st.docs.filter((d) => !d.superseded_at);
    const old = st.docs.filter((d) => d.superseded_at);
    const fixed = st.types.filter((t) => t.id !== 'OTHER').map((t) => row(t.id, t.label, cur.find((d) => d.doc_type === t.id)));
    const others = cur.filter((d) => d.doc_type === 'OTHER').map((d) => row('OTHER', `Other · ${d.label}`, d));
    root.replaceChildren(
      h('h3', null, 'Documents'),
      h('p', { class: 'muted small' }, `PDF, JPG or PNG, up to ${st.max_mb} MB each. Replacing a file keeps the earlier one on record. Every upload and download is logged.`),
      h('ul', { class: 'edocs-list' }, [...fixed, ...others]),
      h('div', { class: 'edoc-more' }, h('button', { class: 'btn', type: 'button', onClick: () => addDialog('OTHER') }, icon('add'), 'Add another document')),
      old.length ? h('details', { class: 'edoc-old' }, h('summary', null, `Earlier versions (${old.length})`),
        h('ul', null, old.map((d) => h('li', null, fileLink(d), h('span', { class: 'faint' }, ` · ${d.type_label}${d.label ? ' · ' + d.label : ''} · replaced ${when(d.superseded_at)} by ${d.superseded_by}`))))) : null);
  }

  function addDialog(type, label = '') {
    const typeName = st.types.find((t) => t.id === type).label;
    const file = h('input', { id: 'ed-file', type: 'file', accept: '.pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png' });
    const what = type === 'OTHER' ? h('input', { id: 'ed-label', type: 'text', maxlength: '60', value: label, placeholder: 'e.g. EPFO, ID proof', class: 'upper' }) : null;
    const exp = h('input', { id: 'ed-exp', type: 'date' });
    const f = (id, lbl, el) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, lbl), el);
    openModal({
      title: `${typeName} · ${key}`, lead: `PDF, JPG or PNG, up to ${st.max_mb} MB.`,
      body: h('div', null, f('ed-file', 'File', file), what ? f('ed-label', 'What is it', what) : null, f('ed-exp', 'Valid until (optional)', exp)),
      actions: [{ label: 'Cancel' }, { label: 'Upload', primary: true, icon: 'upload', onClick: async () => {
        const fl = file.files[0];
        if (!fl) throw new Error('Choose a file first.');
        if (fl.size > st.max_mb * 1048576) throw new Error(`That file is larger than ${st.max_mb} MB.`);
        if (what && !what.value.trim()) throw new Error('Say what this document is.');
        await upload(`${base}/upload`, fl, { 'X-Doc-Type': type, 'X-Label': encodeURIComponent(what?.value.trim() || ''), 'X-Expiry': exp.value });
        toast('Document saved');
        await load();
      } }],
    });
  }

  function removeDialog(d) {
    openModal({
      title: `Remove ${d.type_label}${d.label ? ' · ' + d.label : ''}`, lead: 'The file is taken off the current list. It is not erased: it stays under Earlier versions.',
      actions: [{ label: 'Cancel' }, { label: 'Remove', danger: true, onClick: async () => { await send(`${base}/remove`, { doc_id: d.doc_id }); toast('Document removed'); await load(); } }],
    });
  }

  load();
  return root;
}
