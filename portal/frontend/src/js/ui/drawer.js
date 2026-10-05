import { h, icon } from '../core/dom.js';

let current = null;

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

/** Floating, rounded, draggable detail window centred on screen, with focus trap, Esc to close, scrim click to close, and focus return.
 *  `wide` gives the large fixed-size window used for full records; `tabs` / `foot` are optional fixed strips above and below the scrolling body. */
export function openDrawer({ title, subtitle, onClose, wide = false }) {
  if (current) current.close(true);
  const previous = document.activeElement;
  const body = h('div', { class: 'drawer-b' });
  const titleEl = h('h2', { id: 'drawer-title' }, title);
  const subEl = h('div', { class: 'muted' }, subtitle || '');
  const closeBtn = h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close details' }, icon('close', 'lg'));
  const tabs = h('div', { class: 'drawer-tabs' });
  const foot = h('div', { class: 'drawer-f' });
  const panel = h('div', { class: 'drawer' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'drawer-title' }, h('div', { class: 'drawer-h' }, h('div', { class: 't' }, titleEl, subEl), closeBtn), tabs, body, foot);
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
  const head = panel.querySelector('.drawer-h');
  let drag = null;
  head.addEventListener('pointerdown', (e) => {
    if (e.target.closest('button') || e.button !== 0) return;
    const r = panel.getBoundingClientRect();
    Object.assign(panel.style, { right: 'auto', bottom: 'auto', margin: '0', left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px' });
    drag = { x: e.clientX, y: e.clientY, l: r.left, t: r.top };
    panel.classList.add('dragging'); head.setPointerCapture(e.pointerId);
  });
  head.addEventListener('pointermove', (e) => {
    if (!drag) return;
    panel.style.left = Math.max(0, Math.min(innerWidth - 120, drag.l + e.clientX - drag.x)) + 'px';
    panel.style.top = Math.max(0, Math.min(innerHeight - 48, drag.t + e.clientY - drag.y)) + 'px';
  });
  head.addEventListener('pointerup', () => { drag = null; panel.classList.remove('dragging'); });
  document.addEventListener('keydown', onKey, true);
  scrim.addEventListener('click', () => close());
  closeBtn.addEventListener('click', () => close());
  closeBtn.focus();
  current = { close, body, setTitle(t, s) { titleEl.textContent = t; subEl.textContent = s || ''; },
    setTabs(node) { tabs.replaceChildren(...(node ? [node] : [])); }, setFoot(node) { foot.replaceChildren(...(node ? [node] : [])); } };
  return current;
}
export const closeDrawer = () => current?.close(true);
