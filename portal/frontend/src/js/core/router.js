/** Hash router: #/<view>/<sub>?key=value. State lives in the URL so every filtered view is linkable and Back works. */

const subs = new Set();

// no page visited yet this session (fresh sign-in or the bare root URL): everyone lands on the call tracker
const defaultPath = () => '/dashboard/calls';

export function current() {
  const raw = location.hash.replace(/^#/, '') || defaultPath();
  const [path, qs = ''] = raw.split('?');
  return { path: path.split('/').filter(Boolean), params: new URLSearchParams(qs), raw };
}

export function href(path, params) {
  const q = params ? new URLSearchParams(params).toString() : '';
  return '#/' + path.replace(/^\//, '') + (q ? '?' + q : '');
}

function notify() { for (const fn of subs) fn(current()); }

export const go = (path, params, { replace = false } = {}) => {
  const target = href(path, params);
  if (replace) { history.replaceState(null, '', target); notify(); } else location.hash = target;
};

/** Replace the whole query string without re-rendering the view (used while typing / filtering). */
export function setParams(params) {
  const { path } = current();
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && (v !== '' || k.startsWith('f.'))) q.set(k, v);
  history.replaceState(null, '', '#/' + path.join('/') + (q.toString() ? '?' + q : ''));
}

/** Change one query parameter in place, without re-rendering. */
export function patchParam(key, value) {
  const { path, params } = current();
  if (value == null || value === '') params.delete(key); else params.set(key, value);
  history.replaceState(null, '', '#/' + path.join('/') + (params.toString() ? '?' + params : ''));
}

export const onRoute = (fn) => { subs.add(fn); return () => subs.delete(fn); };
window.addEventListener('hashchange', notify);
export const start = notify;
