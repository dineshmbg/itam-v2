// Floating windows. A <dialog> element, but opened with .show() rather than .showModal(): native top-layer modals put the dialog in
// the same stacking surface the browser uses for a <select>'s own dropdown list, and on some Chromium builds that makes the list
// render behind the dialog that opened it (seen with the engineer picker in "Assign engineer" and any other dialog with a <select> -
// the drawer already avoided this the same way, see openDrawer in drawer.js). .show() keeps the dialog as an ordinary element, so a
// manual scrim, focus trap and Esc handler take over the job the browser did automatically for showModal() - the same pattern
// openDrawer() already uses.
import { h, icon } from '../core/dom.js';

const open = new Set();
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

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
  const dlg = h('dialog', { class: 'modal' + (wide ? ' wide' : '') + (large ? ' large' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'modal-title' },
    h('div', { class: 'modal-h' }, h('h2', { id: 'modal-title' }, title), closeBtn),
    h('div', { class: 'modal-b' }, lead ? h('p', { class: 'muted' }, lead) : null, body, err),
    actions.length ? h('div', { class: 'modal-actions' }, btns) : null);
  const scrim = h('div', { class: 'scrim' });
  function setBusy(b) { btns.forEach((x) => { x.disabled = b; }); dlg.classList.toggle('busy', b); }
  let closed = false;
  // Cleanup runs once, however the dialog ends up closed: through api.close() (the normal path), or - belt and suspenders, since
  // .show()'d dialogs don't reliably fire the native 'close' event on every browser - whenever the element actually leaves open state.
  function cleanup() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    open.delete(api);
    scrim.remove(); dlg.remove();
    if (previous && previous.isConnected && !locked) previous.focus();
    onClose?.();
  }
  const api = {
    el: dlg,
    close() { if (dlg.open) dlg.close(); cleanup(); },
    setBusy,
    error(msg) { err.textContent = msg; err.hidden = false; },
    clearError() { err.hidden = true; },
  };
  function onKey(e) {
    if (e.key === 'Escape') { if (!locked) { e.stopPropagation(); api.close(); } }
    else if (e.key === 'Tab') {
      const f = [...dlg.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  dlg.addEventListener('close', cleanup);
  closeBtn?.addEventListener('click', () => api.close());
  scrim.addEventListener('click', () => { if (!locked) api.close(); });
  document.body.append(scrim, dlg);
  document.addEventListener('keydown', onKey, true);
  open.add(api);
  dlg.show();
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
