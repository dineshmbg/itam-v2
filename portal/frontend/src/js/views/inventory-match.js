// Inventory match: upload a report received from the centre (BigFix, antivirus, patch...) and match it with the asset register by Asset (CI) = computer name.
// Nothing is loaded into the register. The analysis is stored so it can be reopened, and downloaded for the team (Excel) and for management (PDF).
import { download, get, send, upload } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../ui/toast.js';
import { int, pct, titleCase, when } from '../core/format.js';
import { errorBlock, kpiStrip, pageHead, panel, barRows } from './common.js';
import { openModal } from '../ui/modal.js';

const SEV = { Critical: 'bad', 'Needs attention': 'warn', Good: 'ok', Information: 'info' };
const FIELDS = [['name', 'Computer name', true], ['ip', 'IP address'], ['os', 'Operating system'], ['seen', 'Last report / check-in time'], ['type', 'Device type'], ['group', 'Group / domain']];
const PRIORITY = { High: 'bad', Medium: 'warn', Low: 'mute' };

export function mountInventoryMatch(root) {
  let dead = false, meta = null, staged = [], result = null, toolEdited = false;
  const body = h('div', { class: 'grid' });
  const tools = h('div', { class: 'btn-row' });
  root.append(h('div', { class: 'page' }, pageHead('Inventory match', 'Upload a report or inventory received from the centre. Each machine in the asset register is checked by Asset (CI) = computer name: found means the tool is installed, missing means it is not.', tools), body));

  const tool = h('input', { id: 'mt-tool', type: 'text', value: '', placeholder: 'detected from the file', maxlength: 40 });
  tool.addEventListener('input', () => { toolEdited = true; });
  const prefix = h('input', { id: 'mt-prefix', type: 'text', value: 'ANK', maxlength: 40 });
  const target = h('input', { id: 'mt-target', type: 'number', value: 95, min: 1, max: 100 });
  const file = h('input', { id: 'mt-file', type: 'file', accept: '.xlsx,.xlsm,.csv,.txt', multiple: true });
  const classes = new Set();
  const stepEl = h('div', { class: 'stack-v' });
  const out = h('div', { class: 'stack-v' });

  async function load() {
    try { meta = await get('/api/match/meta'); meta.default_classes.forEach((c) => classes.add(c)); if (!dead) draw(); } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, load)); }
  }

  const frow = (id, label, el, hint) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), el, hint ? h('div', { class: 'hint' }, hint) : null);

  function draw() {
    const chips = h('div', { class: 'btn-row', role: 'group', 'aria-label': 'Asset classes to check' }, meta.classes.map((c) => {
      const cb = h('input', { type: 'checkbox', checked: classes.has(c), id: 'mt-c-' + c });
      cb.onchange = () => (cb.checked ? classes.add(c) : classes.delete(c));
      return h('label', { class: 'opt', for: 'mt-c-' + c }, cb, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, titleCase(c.replace('_', ' '))));
    }));
    body.replaceChildren(
      panel('1 · Upload the report', { cls: 'span-5' }, h('div', null,
        frow('mt-file', 'File(s) from the centre (.xlsx or .csv)', file, 'Exactly as received. If the centre sent one fleet in several files (for example one per group), select them all together. Nothing is loaded into the register.'),
        frow('mt-tool', 'Name of the tool', tool, 'The portal works out the product from the columns, whatever the file is called. Type a name only if you want a different one in the report.'),
        frow('mt-prefix', 'Site prefix of the Asset (CI)', prefix, 'Only register machines starting with this are checked (several allowed: ANK, ANKA).'),
        h('div', { class: 'frow' }, h('span', { class: 'flabel' }, 'Asset classes to check'), chips, h('div', { class: 'hint' }, 'Printers, switches and UPS units cannot run an agent, so they are off by default.')),
        frow('mt-target', 'Target coverage (%)', target),
        h('p', { class: 'hint' }, 'When the match finishes it is shared automatically: every engineer sees their own machines under Reports > My inventory report, and is e-mailed their own list.'),
        h('button', { class: 'btn primary', type: 'button', onClick: doUpload }, icon('upload'), 'Read the file(s)'))),
      panel('2 · Check the columns and match', { cls: 'span-7' }, stepEl),
      h('div', { class: 'span-12 stack-v' }, out),
      panel('Earlier analyses', { cls: 'span-12', flush: true }, history()));
    drawStep();
  }

  async function doUpload() {
    const files = [...file.files];
    if (!files.length) { toast('Choose the file received from the centre.', 'bad'); return; }
    stepEl.replaceChildren(h('div', { class: 'loading-line' }, 'Reading and profiling the file…'));
    try {
      staged = [];
      for (const f of files) staged.push(await upload('/api/match/upload', f));
      if (!prefix.value) prefix.value = staged[0].prefixes[0]?.[0] || '';
      drawStep();
    } catch (e) { staged = []; stepEl.replaceChildren(errorBlock(e)); }
  }

  function drawStep() {
    if (!staged.length) { stepEl.replaceChildren(h('p', { class: 'muted' }, 'Upload a file to start.')); return; }
    const first = staged[0];
    const mixed = new Set(staged.map((x) => x.product)).size > 1;
    const sels = {};
    const rows = FIELDS.map(([k, label, req]) => {
      const sel = h('select', { id: 'mt-m-' + k }, h('option', { value: '' }, req ? 'Choose…' : '(not in this file)'), ...first.headers.map((x) => h('option', { value: x, selected: first.mapping[k] === x }, x)));
      sels[k] = sel;
      return frow('mt-m-' + k, label + (req ? ' *' : ''), sel);
    });
    const cards = staged.map((f) => h('div', { class: 'rec-note ' + (f.product === 'generic' ? 'warn' : 'ok') },
      icon(f.product === 'generic' ? 'warning--alt--filled' : 'checkmark--filled'),
      h('div', null, h('strong', null, f.label), ' · ', f.filename, h('div', { class: 'hint' }, `${int(f.rows)} rows · recognised because it ${f.why}`),
        f.groups.length ? h('div', { class: 'hint' }, 'Groups in the file: ', f.groups.map(([g, n]) => `${g} (${int(n)})`).join(', ')) : null,
        f.report_time ? h('div', { class: 'hint' }, 'Newest check-in in the file: ', f.report_time) : null)));
    const pre = first.prefixes.length ? h('p', { class: 'hint' }, 'Most common name prefixes in the file: ', first.prefixes.slice(0, 8).map(([p, n]) => `${p} (${int(n)})`).join(', '), '.') : null;
    stepEl.replaceChildren(...[...cards, mixed ? h('p', { class: 'rec-note bad', role: 'alert' }, icon('error--filled'), 'These files are different products. Upload and analyse each product separately.') : null,
      h('p', { class: 'hint' }, 'Columns used (taken from the first file; change them if they are wrong):'), ...rows, pre,
      h('button', { class: 'btn primary', type: 'button', disabled: mixed, onClick: () => doRun(sels) }, icon('analytics'), 'Match against the register')].filter(Boolean));
  }

  async function doRun(sels) {
    const mapping = Object.fromEntries(Object.entries(sels).map(([k, s]) => [k, s.value]).filter(([, v]) => v));
    if (!mapping.name) { toast('Choose the computer name column.', 'bad'); return; }
    if (!classes.size) { toast('Choose at least one asset class.', 'bad'); return; }
    out.replaceChildren(h('div', { class: 'loading-line' }, 'Matching…'));
    try {
      result = await send('/api/match/run', { stage_ids: staged.map((x) => x.stage_id), mapping, params: { tool: tool.value.trim(), prefix: prefix.value, classes: [...classes], target: Number(target.value) || 95 } });
      showResult(); load2();
    } catch (e) { out.replaceChildren(errorBlock(e)); }
  }

  async function load2() { try { meta = { ...meta, ...(await get('/api/match/meta')) }; } catch { /* history is a convenience */ } }

  async function open(id) {
    out.replaceChildren(h('div', { class: 'loading-line' }, 'Opening…'));
    try { result = await get('/api/match/runs/' + id); showResult(); out.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (e) { out.replaceChildren(errorBlock(e)); }
  }

  const MAILSTATE = { READY: ['info', 'Would be sent'], SENT: ['ok', 'Sent'], FAILED: ['bad', 'Failed'], ALREADY_SENT: ['mute', 'Already sent'], NO_ADDRESS: ['warn', 'No e-mail address'], NO_ENGINEER: ['warn', 'No engineer in the register'], NOTHING_URGENT: ['mute', 'Nothing urgent'] };

  // What happened automatically after the match: shared in the portal, and one e-mail per engineer. Read-only - there is nothing to press.
  function mailPanel(mail) {
    if (!mail) return null;
    const sent = mail.plan.filter((x) => x.status === 'SENT').length;
    const note = mail.error ? h('p', { class: 'rec-note warn', role: 'alert' }, icon('warning--alt--filled'), `Shared in the portal, but no e-mail was sent: ${mail.error} The list below is who would have been mailed.`)
      : h('p', { class: 'hint' }, `Shared in the portal (Reports > My inventory report) and e-mailed to ${sent} engineer${sent === 1 ? '' : 's'}. Each got only their own machines and their own Excel file.`);
    return panel('Sent to engineers', { cls: 'span-12', flush: true, hint: 'automatic' }, h('div', { style: { padding: '12px 16px 0' } }, note),
      mail.plan.length ? table([['name', 'Engineer', null, (r) => titleCase(r.name)], ['email', 'Address', null, (r) => r.email || '—'], ['machines', 'Machines', 'right'], ['missing', 'No agent', 'right'], ['silent', 'Need attention', 'right'],
        ['status', 'Status', null, (r) => h('span', { class: 'badge ' + (MAILSTATE[r.status] || MAILSTATE.READY)[0], title: r.error || '' }, (MAILSTATE[r.status] || MAILSTATE.READY)[1])]], mail.plan, 200) : null);
  }

  const dl = (fmt) => async () => { try { await download('/api/match/export', { run_id: result.run_id, format: fmt }, 'coverage.' + fmt); } catch (e) { toast(e.message, 'bad'); } };

  const table = (cols, rows, limit = 100) => (rows.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
    h('thead', null, h('tr', null, cols.map((c) => h('th', { class: c[2] || null }, c[1])))),
    h('tbody', null, rows.slice(0, limit).map((r) => h('tr', null, cols.map(([k, , cls, fn]) => h('td', { class: cls || null }, fn ? fn(r) : (r[k] ?? '')))))),
  ), rows.length > limit ? h('div', { class: 'hint', style: { padding: '8px 12px' } }, `Showing the first ${int(limit)} of ${int(rows.length)}. The Excel file has them all.`) : null)
    : h('div', { class: 'muted', style: { padding: '16px' } }, 'Nothing here.'));

  const kv = (k, v) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)];
  const prio = (r) => (r.priority ? h('span', { class: 'badge ' + PRIORITY[r.priority] }, r.priority) : '');
  const covCell = (r) => h('span', { class: r.coverage >= result.summary.target ? 'ok' : r.coverage >= result.summary.target - 15 ? 'warn' : 'bad' }, pct(r.coverage));
  const group = (label, rows) => table([['label', label], ['total', 'Machines', 'right'], ['installed', 'In report', 'right'], ['missing', 'Not in report', 'right'], ['high', 'Missing, in use', 'right'], ['coverage', 'Coverage', 'right', covCell], ['attention', 'Need attention', 'right'], ['healthy_pct', 'Healthy', 'right', (r) => pct(r.healthy_pct)]],
    rows.map((r) => ({ ...r, label: label === 'Class' || label === 'Engineer' ? titleCase(r.label) : r.label })), 60);

  function showResult() {
    const s = result.summary, t = s.tool;
    tools.replaceChildren(h('button', { class: 'btn', type: 'button', onClick: dl('xlsx') }, icon('document--export'), 'Excel for the team'), h('button', { class: 'btn primary', type: 'button', onClick: dl('pdf') }, icon('document--pdf'), 'PDF for management'));
    const tone = s.rag === 'green' ? 'ok' : s.rag === 'amber' ? 'warn' : 'bad';
    const missing = result.assets.filter((r) => !r.installed).sort((a, b) => ['High', 'Medium', 'Low'].indexOf(a.priority) - ['High', 'Medium', 'Low'].indexOf(b.priority));
    const attn = result.assets.filter((r) => r.health === 'Needs attention').sort((a, b) => (b.r_age || 0) - (a.r_age || 0));
    const tabs = [
      ['Not installed', missing.length, () => table([['ci', 'Asset (CI)'], ['class', 'Class', null, (r) => titleCase(r.class)], ['model', 'Model'], ['status', 'Status', null, (r) => titleCase(r.status.replace(/_/g, ' '))], ['user', 'User', null, (r) => titleCase(r.user)], ['location', 'Location'],
        ['engineer', 'Engineer', null, (r) => titleCase(r.engineer)], ['priority', 'Priority', null, prio], ['action', 'Action', 'wrap wide']], missing)],
      ['Installed, needs attention', attn.length, () => table([['ci', 'Asset (CI)'], ['class', 'Class', null, (r) => titleCase(r.class)], ['user', 'User', null, (r) => titleCase(r.user)], ['location', 'Location'], ['engineer', 'Engineer', null, (r) => titleCase(r.engineer)],
        ['r_seen', 'Last report'], ['issues', 'Problem', 'wrap wide'], ['action', 'Action', 'wrap wide']], attn)],
      ['Probably the same machine', missing.filter((r) => r.possible).length, () => table([['ci', 'Asset (CI) in register'], ['possible', 'Name in report'], ['possible_by', 'Because', 'wrap'], ['action', 'Action', 'wrap wide']], missing.filter((r) => r.possible))],
      ['In report, not in register', result.unregistered.filter((u) => u.kind !== 'mismatch').length, () => table([['name', 'Name in report'], ['ip', 'IP'], ['os', 'OS'], ['group', 'Group'], ['seen', 'Last report'], ['why', 'Finding', 'wrap wide'], ['action', 'Action', 'wrap wide']], result.unregistered.filter((u) => u.kind !== 'mismatch'))],
      ['Data checks', (s.checks || []).filter((c) => c.status === 'warn').length, () => table([['status', 'Result', null, (r) => h('span', { class: 'badge ' + ({ ok: 'ok', warn: 'warn', info: 'info' }[r.status] || 'mute') }, { ok: 'OK', warn: 'Check', info: 'Note' }[r.status] || r.status)], ['check', 'Check', 'wrap'], ['detail', 'What was found', 'wrap wide'], ['handled', 'How it was handled', 'wrap wide']], s.checks || [])],
      ['How totals add up', (s.recon || []).length, () => table([['label', 'Line', 'wrap'], ['n', 'Count', 'right', (r) => int(r.n)], ['note', 'Note']], s.recon || [])],
    ];
    const pane = h('div', null);
    const bar = h('div', { class: 'mtabs', role: 'tablist' });
    const pick = (i) => { [...bar.children].forEach((b, j) => b.setAttribute('aria-selected', String(i === j))); pane.replaceChildren(tabs[i][2]()); };
    tabs.forEach(([name, n], i) => bar.append(h('button', { class: 'mtab', type: 'button', role: 'tab', onClick: () => pick(i) }, name, h('span', { class: 'count' }, int(n)))));

    out.replaceChildren(
      h('div', { class: 'rec-note ' + tone, role: 'status' }, icon(tone === 'ok' ? 'checkmark--filled' : 'warning--filled'),
        `${t}: ${int(s.installed)} of ${int(s.assets)} machines appear in the centre's report (${pct(s.coverage)}, target ${pct(s.target)}), but only ${int(s.healthy)} (${pct(s.healthy_pct)}) are working properly. ${int(s.missing)} are missing.`),
      kpiStrip([{ id: 'a', label: 'Machines checked', value: int(s.assets), sub: s.classes.map(titleCase).join(', ') }, { id: 'i', label: 'In the report', value: int(s.installed), tone: 'ok', sub: `${int(s.healthy)} healthy` },
        { id: 'm', label: 'Not in the report', value: int(s.missing), tone: s.missing ? 'bad' : 'ok', sub: `${int(s.missing_high)} deployed to users` }, { id: 'c', label: 'Coverage', value: pct(s.coverage), tone, sub: `target ${pct(s.target)}` },
        { id: 'd', label: 'Installed, need attention', value: int(s.attention), tone: s.attention ? 'warn' : null, sub: 'agent not working properly' },
        { id: 'u', label: 'Not in register', value: int(s.unregistered), tone: s.unregistered ? 'warn' : null, sub: 'report names with no CI' }]),
      h('div', { class: 'grid' },
        panel('About this report', { cls: 'span-12', hint: s.reconciled === false ? 'a total did not reconcile' : 'every total reconciles to the file and the register' }, h('div', { class: 'kv' },
          kv('Product', `${s.product_label || t}${s.tool !== s.product_label ? ' (shown as ' + s.tool + ')' : ''}`), kv('File(s)', (s.files || []).map((f) => `${f.name} (${int(f.rows)} rows)`).join('; ') || result.filename), kv('Report date', s.report_as_of || s.as_of),
          kv('Checked', `${s.prefixes.join('/')} · ${s.classes.map(titleCase).join(', ')} · ${int(s.assets)} machines`))),
        mailPanel(result.mail),
        panel('Findings and what to do', { cls: 'span-12', flush: true }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, h('th', null, 'Rating'), h('th', null, 'Finding'), h('th', null, 'What to do'))),
          h('tbody', null, s.findings.map((f) => h('tr', null, h('td', null, h('span', { class: 'badge ' + (SEV[f.severity] || 'mute') }, f.severity)), h('td', { class: 'wrap' }, f.finding), h('td', { class: 'wrap' }, f.action))))))),
        panel('By class', { cls: 'span-6', flush: true }, group('Class', s.by_class)), panel('By engineer', { cls: 'span-6', flush: true, hint: 'who has machines to fix' }, group('Engineer', s.by_engineer)),
        panel('By location', { cls: 'span-6', flush: true }, group('Location', s.by_location)),
        panel('Models with most gaps', { cls: 'span-3' }, barRows(s.gap_models.map((m) => ({ label: m.label, n: m.n })), { tone: 'bad' })),
        (s.os_mix || []).length ? panel('Operating systems (installed)', { cls: 'span-3' }, barRows(s.os_mix)) : null,
        panel('Detail', { cls: 'span-12', flush: true, hint: 'the Excel file holds every row' }, bar, pane)));
    pick(0);
  }

  function history() {
    if (!meta.history.length) return h('div', { class: 'muted', style: { padding: '16px' } }, 'No analyses yet.');
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['When', 'Tool', 'File', 'Machines', 'Installed', 'Coverage', 'By', ''].map((x) => h('th', null, x)))),
      h('tbody', null, meta.history.map((r) => h('tr', null, h('td', { class: 'nowrap' }, when(r.at)), h('td', null, r.tool), h('td', { class: 'wrap' }, r.filename), h('td', { class: 'right' }, int(+r.assets)), h('td', { class: 'right' }, int(+r.installed)),
        h('td', { class: 'right' }, pct(+r.coverage)), h('td', null, r.username, r.published_at ? h('span', { class: 'badge ok', style: { marginLeft: '6px' } }, 'Shared') : null), h('td', null, h('button', { class: 'link-btn', type: 'button', onClick: () => open(r.run_id) }, 'Open')))))));
  }

  load();
  return { destroy() { dead = true; }, onLive: () => {} };
}
