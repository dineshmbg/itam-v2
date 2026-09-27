const KEY = 'itam.theme';
const mq = window.matchMedia('(prefers-color-scheme: dark)');

export const getTheme = () => document.documentElement.dataset.theme || 'auto';
export const resolved = () => { const t = getTheme(); return t === 'auto' ? (mq.matches ? 'dark' : 'light') : t; };

export function setTheme(t) {
  if (!['light', 'dark', 'auto'].includes(t)) return;
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem(KEY, t); } catch (_) { /* private mode: keep the choice for this session only */ }
  window.dispatchEvent(new CustomEvent('themechange', { detail: resolved() }));
}

mq.addEventListener('change', () => {
  if (getTheme() === 'auto') window.dispatchEvent(new CustomEvent('themechange', { detail: resolved() }));
});

export const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
