// Edit schema (which fields exist, which are choices, what the signed-in group may change) and small notifications.
import { get } from './api.js';
import { h } from './dom.js';

let schemaP;
export const getSchema = () => (schemaP ||= get('/api/edit/schema').catch((e) => { schemaP = null; throw e; }));
export const resetSchema = () => { schemaP = null; };

export function toast(text, tone) {
  const t = h('div', { class: 'toast' + (tone ? ' ' + tone : ''), role: 'status' }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), 3600);
}
