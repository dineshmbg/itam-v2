// Small transient notification (bottom of the window; the message goes away by itself).
import { h } from '../core/dom.js';

export function toast(text, tone) {
  const t = h('div', { class: 'toast' + (tone ? ' ' + tone : ''), role: 'status' }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), 3600);
}
