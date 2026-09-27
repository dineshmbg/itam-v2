/** Server-Sent Events: the server pushes a 'change' the moment any inventory table changes in PostgreSQL. */
const listeners = new Set();
const stateSubs = new Set();
let state = 'connecting';
let lastChange = null;
let es;
const authSubs = new Set();
export const onAuth = (fn) => { authSubs.add(fn); return () => authSubs.delete(fn); };

function setState(s) { if (s !== state) { state = s; stateSubs.forEach((f) => f(state)); } }
export const getState = () => state;
export const getLast = () => lastChange;
export const onChange = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
export const onState = (fn) => { stateSubs.add(fn); return () => stateSubs.delete(fn); };

export function stop() { if (es) { es.close(); es = null; } setState('connecting'); }

export function start() {
  stop();
  es = new EventSource('/api/events');
  es.addEventListener('auth', () => { stop(); authSubs.forEach((f) => f()); });
  es.addEventListener('hello', () => setState('on'));
  es.addEventListener('change', (e) => {
    setState('on');
    const d = JSON.parse(e.data);
    lastChange = d.ts * 1000;
    listeners.forEach((f) => f(d));
  });
  es.onopen = () => setState('on');
  es.onerror = () => setState(es.readyState === EventSource.CLOSED ? 'off' : 'connecting');
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && es && state !== 'on') start();
});
