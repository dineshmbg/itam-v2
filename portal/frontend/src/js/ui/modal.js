// Floating windows. Built on the native <dialog> element: focus is trapped, Esc closes (unless `locked`), and focus returns to the trigger.
import { h, icon } from '../core/dom.js';

const open = new Set();

/** openModal({title, lead, body, actions, locked, wide, large}) -> {el, close(), setBusy(b), error(msg)}. `actions`: [{label, primary, danger, onClick, keepOpen}] */
export function openModal({ title, lead, body, actions = [], locked = false, wide = false, large = false, onClose }) {
  const previous = document.activeElement;
  const err = h('div', { class: 'ferr modal-err', role: 'alert', hidden: true });
  const btns = actions.map((a) => {
    const b = h('button', { class: 'btn' + (a.primary ? ' primary' : '') + (a.danger ? ' danger' : ''), type: 'button' }, a.icon ? icon(a.icon) : null, a.label);
    b.addEventListener('click', async () => {
      if (b.disabled) return;
      err.hidden = true;
      if (!a.onClick) { api.close(); return; }
      setBusy(true);
      try {
        const r = await a.onClick(api);
        if (r !== false && !a.keepOpen) api.close();
      } catch (e) { api.error(e.message || 'Something went wrong.'); }
      finally { setBusy(false); }
    });
    return b;
  });
  const closeBtn = locked ? null : h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close' }, icon('close', 'lg'));
  const dlg = h('dialog', { class: 'modal' + (wide ? ' wide' : '') + (large ? ' large' : ''), 'aria-labelledby': 'modal-title' },
    h('div', { class: 'modal-h' }, h('h2', { id: 'modal-title' }, title), closeBtn),
    h('div', { class: 'modal-b' }, lead ? h('p', { class: 'muted' }, lead) : null, body, err),
    actions.length ? h('div', { class: 'modal-actions' }, btns) : null);
  function setBusy(b) { btns.forEach((x) => { x.disabled = b; }); dlg.classList.toggle('busy', b); }
  const api = {
    el: dlg,
    close() { if (dlg.open) dlg.close(); },
    setBusy,
    error(msg) { err.textContent = msg; err.hidden = false; },
    clearError() { err.hidden = true; },
  };
  dlg.addEventListener('cancel', (e) => { if (locked) e.preventDefault(); });
  dlg.addEventListener('close', () => {
    open.delete(api);
    dlg.remove();
    if (previous && previous.isConnected && !locked) previous.focus();
    onClose?.();
  });
  closeBtn?.addEventListener('click', () => api.close());
  document.body.append(dlg);
  open.add(api);
  dlg.showModal();
  (dlg.querySelector('[autofocus]') || dlg.querySelector('input:not([type=hidden]), select, textarea') || btns[btns.length - 1] || closeBtn)?.focus();
  return api;
}

export function closeAllModals() { [...open].forEach((m) => m.close()); }

/** Yes/no question. Resolves true when confirmed. */
export function confirmBox({ title, lead, confirm = 'Confirm', danger = false, body }) {
  return new Promise((resolve) => {
    let answer = false;
    openModal({ title, lead, body, onClose: () => resolve(answer),
      actions: [{ label: 'Cancel' }, { label: confirm, primary: !danger, danger, onClick: () => { answer = true; } }] });
  });
}
