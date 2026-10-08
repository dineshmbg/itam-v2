// Software update (administrators): install a new version of the portal from a signed release package - like a firmware update. The portal only
// receives, checks and requests; the update service on the VM re-checks everything, backs up, installs, health-checks and rolls back by itself.
import { get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../ui/toast.js';
import { openModal } from '../ui/modal.js';
import { errorBlock, pageHead, panel } from './common.js';
import { when } from './audit.js';

const bytes = (n) => (n < 1048576 ? `${Math.round((n || 0) / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const STEPS = [['verifying', 'Check the package'], ['backup', 'Back up the database'], ['loading', 'Load the new version'], ['restarting', 'Restart the portal'], ['health', 'Wait until it is healthy']];
const RESULT = { success: ['ok', 'checkmark--filled', 'Updated'], rolled_back: ['warn', 'warning--alt--filled', 'Rolled back'], failed: ['bad', 'error--filled', 'Failed'], running: ['info', 'time', 'In progress'], queued: ['info', 'time', 'Waiting'] };

export function mountUpdate(root) {
  let dead = false, data = null, timer = null, offline = 0, uploading = false, pendingFile = false;
  const holder = h('div', { class: 'page' });
  root.append(holder);
  const body = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '16px' } });
  holder.append(pageHead('Software update', 'Install a new version of the portal from a release package. Your data is never replaced: the database is backed up first, and the previous version stays available to go back to.', []), body);

  async function load() {
    if (pendingFile) { schedule(); return; }   // a file is chosen but not yet uploaded - do not rebuild the page (and its empty file input) under the user
    try {
      data = await get('/api/admin/update');
      offline = 0;
      if (dead) return;
      draw();
    } catch (e) {
      offline += 1;
      if (dead) return;
      if (data?.status && ['queued', 'running'].includes(data.status.result)) drawRestarting();   // the portal restarts on purpose in the middle of an update
      else if (!data) body.replaceChildren(errorBlock(e, load));
    }
    schedule();
  }
  function schedule() {
    clearTimeout(timer);
    const active = data?.status && ['queued', 'running'].includes(data.status.result);
    if (!dead && !uploading) timer = setTimeout(load, active || offline ? 2000 : 10000);
  }

  function drawRestarting() {
    const note = h('div', { class: 'rec-note info', role: 'status' }, icon('time'), 'The portal is restarting on the new version. This page reconnects by itself - do not close it.');
    body.prepend(note);
  }

  const kv = (k, v) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)];
  const badge = (tone, ic, text) => h('span', { class: `badge ${tone}` }, icon(ic), text);

  function draw() {
    if (!data.enabled) {
      body.replaceChildren(panel('Not enabled on this server', {}, h('div', { class: 'panel-b' }, h('p', null, 'Software update needs the update service on the VM. Run the installer again (sudo ./vm-install.sh) - it sets this up - and then reload this page. Details: deploy/README.md, "Software update".'))));
      return;
    }
    const st = data.status;
    const active = st && ['queued', 'running'].includes(st.result);
    const parts = [statusPanel(active)];
    if (st) parts.push(progressPanel(st, active));
    if (!active) parts.push(packagesPanel());
    if (data.history?.length) parts.push(historyPanel());
    body.replaceChildren(...parts);
  }

  function statusPanel(active) {
    const a = data.agent;
    const svc = a.alive ? badge('ok', 'checkmark--filled', 'Update service running') : badge('bad', 'error--filled', 'Update service not answering');
    const rb = data.rollback_available && !active ? h('button', { class: 'btn', type: 'button', onClick: rollbackDialog }, icon('undo'), `Go back to ${data.previous_version}`) : null;
    return panel('This portal', {}, h('div', { class: 'panel-b' }, h('div', { class: 'kv' }, ...kv('Running version', h('span', { class: 'mono' }, data.current_version)), ...kv('Previous version', data.previous_version ? h('span', { class: 'mono' }, data.previous_version) : h('span', { class: 'faint' }, 'none')),
      ...kv('Update service on the VM', svc)), !a.alive ? h('p', { class: 'rec-note warn' }, icon('warning--alt--filled'), 'Nothing can be installed until the update service on the VM answers (on the VM: systemctl status itam-updater.path itam-updater-heartbeat.timer).') : null, rb ? h('div', { class: 'btn-row' }, rb) : null));
  }

  function progressPanel(st, active) {
    const [tone, ic, label] = RESULT[st.result] || RESULT.running;
    const seenIdx = Math.max(-1, ...(st.steps || []).map((x) => STEPS.findIndex(([k]) => k === x.name)));
    const list = h('ol', { class: 'timeline' }, STEPS.map(([key, text], i) => {
      const done = st.result === 'success' || i < seenIdx;
      const here = i === seenIdx && st.result !== 'success';
      const state = done ? 'done' : here ? (active ? 'now' : 'failed') : 'todo';
      return h('li', null, h('span', { class: 'tl-ico' }, icon(state === 'done' ? 'checkmark--filled' : state === 'failed' ? 'error--filled' : state === 'now' ? 'time' : 'circle-dash')),
        h('div', null, h('strong', null, text), state === 'now' ? h('span', { class: 'faint' }, ' …') : null));
    }));
    const rolled = (st.steps || []).some((s) => s.name === 'rollback');
    return panel(`${st.version_from || '?'} → ${st.version_to || '?'}`, { hint: `requested by ${st.requested_by || '?'}${st.requested_at ? ' · ' + when(st.requested_at) : ''}` },
      h('div', { class: 'panel-b' }, h('div', { style: { marginBottom: '8px' } }, badge(tone, ic, label)), h('p', null, st.message || ''), list,
        rolled ? h('p', { class: 'muted small' }, 'The new version did not start correctly, so the previous version was restored automatically.') : null,
        data.log?.length ? h('details', { open: active }, h('summary', null, 'Detailed log'), h('pre', { class: 'mono small', style: { maxHeight: '260px', overflow: 'auto' } }, data.log.join('\n'))) : null));
  }

  function packagesPanel() {
    const file = h('input', { type: 'file', accept: '.itamrel', id: 'up-file', 'aria-label': 'Release package' });
    file.addEventListener('change', () => { pendingFile = file.files.length > 0; });
    const bar = h('progress', { max: '100', value: '0', hidden: true, style: { width: '100%' } });
    const msg = h('div', { class: 'muted small', role: 'status' }, '');
    const go = h('button', { class: 'btn primary', type: 'button', onClick: () => upload(file, go, bar, msg) }, icon('upload'), 'Upload package');
    const rows = data.packages.map((p) => {
      const sig = p.signature === true ? badge('ok', 'checkmark--filled', 'Signature valid') : p.signature === false ? badge('bad', 'error--filled', 'Signature NOT valid') : badge('mute', 'information', 'Signature is checked on the VM');
      const older = p.version && p.version < data.current_version;
      return h('div', { class: 'integrity-row' }, h('span', { class: p.ok ? 'tick' : 'cross' }, icon(p.ok ? 'checkmark--filled' : 'error--filled', 'lg')),
        h('div', null, h('div', { class: 'ttl' }, p.name), h('div', { class: 'det' }, `Version ${p.version || '?'} · built ${p.built_at || '?'} · ${bytes(p.size)} · uploaded ${when(p.uploaded_at)}`), p.notes ? h('div', { class: 'det' }, p.notes) : null,
          h('div', { style: { marginTop: '4px' } }, sig, p.version === data.current_version ? h('span', { class: 'badge info', style: { marginLeft: '6px' } }, 'This is the running version') : older ? h('span', { class: 'badge warn', style: { marginLeft: '6px' } }, 'Older than the running version') : null),
          p.problems?.length ? h('div', { class: 'note' }, p.problems.join('; ')) : null),
        h('div', { class: 'res' }, p.ok && p.version !== data.current_version ? h('button', { class: 'btn primary', type: 'button', onClick: () => installDialog(p) }, icon('launch'), 'Install…') : null,
          h('button', { class: 'btn ghost', type: 'button', onClick: () => discard(p) }, icon('close--outline'), 'Discard')));
    });
    return panel('Install a new version', { hint: 'Choose the .itamrel file made on the development PC (deploy\\build-release.ps1). You can also copy it to /var/lib/itam-updates/incoming on the VM - it is listed below.' },
      h('div', { class: 'panel-b' }, h('div', { class: 'frow row-inline' }, file, go), bar, msg), h('div', null, rows.length ? rows : h('p', { class: 'muted', style: { padding: '12px 16px' } }, 'No release package on the server yet.')));
  }

  function historyPanel() {
    return panel('Earlier updates', { flush: true }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['When', 'By', 'From', 'To', 'Result'].map((t) => h('th', null, t)))),
      h('tbody', null, data.history.map((r) => { const [tone, ic, label] = RESULT[r.result] || RESULT.failed; return h('tr', null, h('td', { class: 'nowrap' }, r.finished_at ? when(r.finished_at) : ''), h('td', null, r.requested_by || ''), h('td', { class: 'mono' }, r.version_from || ''), h('td', { class: 'mono' }, r.version_to || ''), h('td', null, badge(tone, ic, label), h('div', { class: 'faint small' }, r.message || ''))); })))));
  }

  function upload(fileInput, btn, bar, msg) {
    const f = fileInput.files?.[0];
    if (!f) { toast('Choose the release package first.', 'bad'); return; }
    if (!/^itam-release-.+\.itamrel$/.test(f.name)) { toast('That is not a release package (itam-release-….itamrel).', 'bad'); return; }
    pendingFile = false;   // the selection is being used now - safe to let the page refresh again
    uploading = true; btn.disabled = true; bar.hidden = false; bar.value = 0; msg.textContent = 'Uploading…';
    const x = new XMLHttpRequest();
    x.open('POST', '/api/admin/update/upload');
    x.setRequestHeader('X-Requested-With', 'itam-portal'); x.setRequestHeader('Content-Type', 'application/octet-stream'); x.setRequestHeader('X-Filename', encodeURIComponent(f.name));
    x.upload.onprogress = (e) => { if (e.lengthComputable) { bar.value = (e.loaded / e.total) * 100; msg.textContent = `Uploading… ${bytes(e.loaded)} of ${bytes(e.total)}`; if (e.loaded >= e.total) msg.textContent = 'Checking the package (signature and checksum)…'; } };
    x.onload = () => {
      uploading = false;
      let r = {}; try { r = JSON.parse(x.responseText); } catch (_) { /* ignore */ }
      if (x.status === 200) { toast(`Package ${r.version} is ready to install`); } else { toast(r.error || `Upload failed (${x.status})`, 'bad'); }
      load();
    };
    x.onerror = () => { uploading = false; toast('The upload was interrupted. Check the connection and try again.', 'bad'); load(); };
    x.send(f);
  }

  async function discard(p) {
    try { await send('/api/admin/update/discard', { package: p.name }); toast('Package removed'); load(); } catch (e) { toast(e.message, 'bad'); }
  }

  function installDialog(p) {
    const older = p.version < data.current_version;
    const c = h('input', { id: 'up-c', class: 'no-upper', type: 'text', autocomplete: 'off', placeholder: 'UPDATE' });
    const ol = h('input', { type: 'checkbox', id: 'up-old' });
    openModal({
      title: `Install version ${p.version}`, lead: 'The portal will be unavailable for about a minute while it restarts. Everyone stays signed in.',
      body: h('div', null, h('div', { class: 'kv' }, ...kv('From', h('span', { class: 'mono' }, data.current_version)), ...kv('To', h('span', { class: 'mono' }, p.version)), ...kv('Package', p.name)),
        h('div', { class: 'dsec' }, h('h3', null, 'What will happen'), h('ol', null, ['The update service checks the signature and the checksum again.', 'The database is backed up.', 'The new version is loaded and the portal restarts.',
          'It waits until the new version is healthy. If it is not, the previous version is restored automatically.'].map((t) => h('li', null, t)))),
        older ? h('label', { class: 'opt', for: 'up-old' }, ol, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, 'I understand this is an OLDER version than the one running')) : null,
        h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'up-c' }, 'Type UPDATE to confirm'), c)),
      actions: [{ label: 'Cancel' }, { label: 'Install', primary: true, onClick: async () => {
        await send('/api/admin/update/start', { package: p.name, confirm: c.value, allow_older: ol.checked });
        toast('Update started'); load();
      } }],
    });
  }

  function rollbackDialog() {
    const c = h('input', { id: 'rb-c', class: 'no-upper', type: 'text', autocomplete: 'off', placeholder: 'ROLLBACK' });
    openModal({
      title: `Go back to version ${data.previous_version}`, lead: 'Use this if the current version misbehaves. The database is backed up first; your data is kept (new columns added by the newer version are simply ignored).',
      body: h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'rb-c' }, 'Type ROLLBACK to confirm'), c),
      actions: [{ label: 'Cancel' }, { label: 'Go back', danger: true, onClick: async () => { await send('/api/admin/update/rollback', { confirm: c.value }); toast('Rollback started'); load(); } }],
    });
  }

  load();
  return { destroy() { dead = true; clearTimeout(timer); }, onLive: () => {} };
}
