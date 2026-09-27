/** Fetch JSON. Responses carry ETags, so repeat requests are answered with 304 by the browser cache without re-transfer.
 *  A dropped connection is retried once (idle keep-alive sockets are sometimes reset by the OS); a 401 tells the session layer to show the sign-in screen. */
const authLost = new Set();
export const onAuthLost = (fn) => { authLost.add(fn); return () => authLost.delete(fn); };

async function request(url, init, retry = 1) {
  let res;
  try {
    res = await fetch(url, init);
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    if (retry > 0) return request(url, init, retry - 1);
    throw new Error('The portal server could not be reached. Check that it is running.');
  }
  if (res.status === 401) {
    let d = {};
    try { d = await res.clone().json(); } catch (_) { /* ignore */ }
    if (d.code === 'auth') authLost.forEach((fn) => fn(d));
  }
  return res;
}

async function fail(res) {
  let data = {};
  try { data = await res.json(); } catch (_) { /* ignore */ }
  const e = new Error(data.error || res.statusText || 'Request failed');
  e.status = res.status; e.code = data.code; e.fields = data.fields || {}; e.current = data.current || {};
  return e;
}

export async function get(path, params, opts = {}) {
  let url = path;
  if (params) {
    const q = params instanceof URLSearchParams ? params : new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null));
    const s = q.toString();
    if (s) url += (path.includes('?') ? '&' : '?') + s;
  }
  const res = await request(url, { signal: opts.signal, headers: { Accept: 'application/json' } });
  if (!res.ok) throw await fail(res);
  return res.json();
}

/** POST JSON. Carries the marker header the server requires (with SameSite cookies and an Origin check this is the CSRF defence). */
export async function send(path, body) {
  const res = await request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Requested-With': 'itam-portal' },
    body: JSON.stringify(body ?? {}),
  }, 0);
  if (!res.ok) throw await fail(res);
  return res.json();
}

/** Download a generated file (POST so the request body can carry report definitions). Returns after the browser has been handed the file. */
export async function download(path, body, fallbackName = 'download') {
  const res = await request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'itam-portal' },
    body: JSON.stringify(body ?? {}),
  }, 0);
  return saveResponse(res, fallbackName);
}

/** Download a file produced by a GET request (e.g. a register exported as CSV). */
export async function downloadGet(path, params, fallbackName = 'download') {
  const qs = params ? new URLSearchParams(params).toString() : '';
  const res = await request(path + (qs ? '?' + qs : ''), { headers: { 'X-Requested-With': 'itam-portal' } }, 0);
  return saveResponse(res, fallbackName);
}

async function saveResponse(res, fallbackName) {
  if (!res.ok) throw await fail(res);
  const blob = await res.blob();
  const cd = res.headers.get('content-disposition') || '';
  const m = /filename="?([^";]+)"?/i.exec(cd);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = m ? m[1] : fallbackName;
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
  return m ? m[1] : fallbackName;
}

/** Upload one file as the raw request body (data import). */
export async function upload(path, file, headers = {}) {
  const res = await request(path, { method: 'POST', headers: { 'X-Requested-With': 'itam-portal', 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name), ...headers }, body: file }, 0);
  if (!res.ok) throw await fail(res);
  return res.json();
}
