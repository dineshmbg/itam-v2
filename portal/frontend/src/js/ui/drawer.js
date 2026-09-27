import { h, icon } from '../core/dom.js';

let current = null;

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

/** Right-hand detail panel with focus trap, Esc to close, scrim click to close, and focus return. */
export function openDrawer({ title, subtitle, onClose }) {
  if (current) current.close(true);
  const previous = document.activeElement;
  const body = h('div', { class: 'drawer-b' });
  const titleEl = h('h2', { id: 'drawer-title' }, title);
  const subEl = h('div', { class: 'muted' }, subtitle || '');
  const closeBtn = h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close details' }, icon('close', 'lg'));
  const panel = h('div', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'drawer-title' }, h('div', { class: 'drawer-h' }, h('div', { class: 't' }, titleEl, subEl), closeBtn), body);
  const scrim = h('div', { class: 'scrim' });
  document.body.append(scrim, panel);
  document.body.style.overflow = 'hidden';

  function close(silent) {
    document.removeEventListener('keydown', onKey, true);
    scrim.remove(); panel.remove();
    document.body.style.overflow = '';
    current = null;
    if (previous && previous.isConnected) previous.focus();
    if (!silent) onClose?.();
  }
  function onKey(e) {
    if (e.key === 'Escape') { e.stopPropagation(); close(); }
    else if (e.key === 'Tab') {
      const f = [...panel.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  document.addEventListener('keydown', onKey, true);
  scrim.addEventListener('click', () => close());
  closeBtn.addEventListener('click', () => close());
  closeBtn.focus();
  current = { close, body, setTitle(t, s) { titleEl.textContent = t; subEl.textContent = s || ''; } };
  return current;
}
export const closeDrawer = () => current?.close(true);
