// Shared bits of the PM work-order screens: status wording, and the small windows (owner answer, verify, send back, link a call, close a finding).
import { send } from '../core/api.js';
import { h } from '../core/dom.js';
import { toast } from '../core/editor.js';
import { openModal } from '../ui/modal.js';

const today = () => new Date().toISOString().slice(0, 10);
export const frow = (id, label, el, hint) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), el, hint ? h('div', { class: 'hint' }, hint) : null);
export const SEVERITY = { CRITICAL: ['Critical', 'bad'], MAJOR: ['Major', 'warn'], MINOR: ['Minor', 'mute'] };
export const ACK = { PENDING: ['Awaiting owner', 'info'], ACKNOWLEDGED: ['Owner accepted', 'ok'], DEEMED: ['Deemed accepted', 'warn'], DISPUTED: ['Owner disputed', 'bad'], NA: ['No owner step', 'mute'] };

/** [[text, tone], ...] badges for a work-order row. */
export function stateBadges(r) {
  const out = [];
  if (r.state === 'CANCELLED') return [['Cancelled', 'mute']];
  if (r.state === 'CLOSED') out.push(r.legacy ? ['Done before work orders', 'mute'] : ['Closed', 'ok']);
  else if (r.state === 'COMPLETED') {
    if (r.verify_required && !r.verified_by) out.push(['Awaiting verification', 'warn']);
    if (r.ack_state === 'PENDING') out.push(['Awaiting owner', 'info']);
  } else if (r.overdue) out.push([r.state === 'IN_PROGRESS' ? 'In progress, overdue' : 'Overdue', 'bad']);
  else out.push(r.state === 'IN_PROGRESS' ? ['In progress', 'info'] : ['To do', 'mute']);
  if (r.defer_status === 'REQUESTED') out.push(['Deferral requested', 'warn']);
  if (r.defer_status === 'APPROVED' && r.state !== 'CLOSED') out.push(['Deferred', 'mute']);
  if (r.ack_state === 'DISPUTED') out.push(['Owner disputed', 'bad']);
  return out;
}


/** Record the owner's answer (an administrator does it on the owner's behalf until owners can sign in). */
export function ownerDialog({ ids, owner, onDone, dispute = false }) {
  const by = h('input', { id: 'ow-by', type: 'text', maxlength: '80', class: 'no-upper', value: owner || '', placeholder: 'Who confirmed (name, section)' });
  const note = h('textarea', { id: 'ow-note', maxlength: '300', placeholder: dispute ? 'What does the owner say is wrong?' : 'Optional: how it was confirmed (phone, e-mail, in person)' });
  openModal({
    title: dispute ? `The owner disputes this PM${ids.length > 1 ? ` (${ids.length} work orders)` : ''}` : `Owner accepts the PM${ids.length > 1 ? ` (${ids.length} work orders)` : ''}`,
    lead: dispute ? 'The work order goes back to the engineer with a finding.' : 'You are recording the owner\'s confirmation on their behalf. It is logged with your name.',
    body: h('div', null, frow('ow-by', 'Owner / section that answered', by), frow('ow-note', dispute ? 'What is disputed' : 'Note', note)),
    actions: [{ label: 'Cancel' }, { label: dispute ? 'Send back to engineer' : 'Record acceptance', primary: !dispute, danger: dispute, onClick: async () => {
      const r = await send('/api/pmwo/ack', { ids, action: dispute ? 'DISPUTE' : 'ACK', by: by.value, note: note.value });
      report(r, dispute ? 'sent back' : 'accepted');
      onDone?.(r);
    } }],
  });
}

export function verifyDialog({ ids, approve, onDone }) {
  const note = h('textarea', { id: 'vf-note', maxlength: '300', placeholder: approve ? 'Optional: what you checked' : 'Why is it being sent back?' });
  openModal({
    title: approve ? `Verify ${ids.length} work order${ids.length > 1 ? 's' : ''}` : 'Send back to the engineer',
    lead: approve ? 'Confirm the work was done properly. You cannot verify work you completed yourself.' : 'The work order returns to In progress and must be completed again.',
    body: frow('vf-note', approve ? 'Note' : 'Reason', note),
    actions: [{ label: 'Cancel' }, { label: approve ? 'Verify' : 'Send back', primary: approve, danger: !approve, icon: approve ? 'checkmark' : null, onClick: async () => {
      const r = await send('/api/pmwo/verify', { ids, approve, note: note.value });
      report(r, approve ? 'verified' : 'sent back');
      onDone?.(r);
    } }],
  });
}

export function report(r, word) {
  const n = r.done ?? r.completed ?? 0;
  toast(`${n} work order${n === 1 ? '' : 's'} ${word}${r.skipped?.length ? ` - ${r.skipped.length} skipped: ${r.skipped[0].wo_no} (${r.skipped[0].reason})` : ''}`, r.skipped?.length ? 'warn' : undefined);
}

export function linkCallDialog({ finding, onDone }) {
  const sr = h('input', { id: 'fc-sr', type: 'text', maxlength: '40', class: 'no-upper', placeholder: 'SR ID from the CIPL tracker' });
  openModal({
    title: `Link a call to this finding`, lead: 'Raise the call with CIPL first. The portal only links an SR ID that is already in the call tracker.',
    body: frow('fc-sr', 'SR ID', sr),
    actions: [{ label: 'Cancel' }, { label: 'Link call', primary: true, onClick: async () => { await send('/api/pmwo/finding', { id: finding.finding_id, sr_id: sr.value }); toast('Call linked'); onDone?.(); } }],
  });
}

export function closeFindingDialog({ finding, onDone }) {
  const note = h('textarea', { id: 'fc-note', maxlength: '300', placeholder: 'What was done to resolve it?' });
  openModal({
    title: 'Close this finding', body: frow('fc-note', 'Resolution', note),
    actions: [{ label: 'Cancel' }, { label: 'Close finding', primary: true, onClick: async () => { await send('/api/pmwo/finding', { id: finding.finding_id, close: true, note: note.value }); toast('Finding closed'); onDone?.(); } }],
  });
}

export async function setSeverity(finding, severity, onDone) {
  await send('/api/pmwo/finding', { id: finding.finding_id, severity });
  toast('Severity changed');
  onDone?.();
}

export { today };
