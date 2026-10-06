// Inventory match: upload a report received from the centre (BigFix, antivirus, patch...) and match it with the asset register by Asset (CI) = computer name.
// Nothing is loaded into the register. The analysis is stored so it can be reopened, and downloaded for the team (Excel) and for management (PDF).
import { download, get, send, upload } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { toast } from '../core/editor.js';
import { int, pct, titleCase } from '../core/format.js';
import { errorBlock, kpiStrip, pageHead, panel, barRows } from './common.js';
import { when } from './audit.js';
import { openModal } from '../ui/modal.js';

const SEV = { Critical: 'bad', 'Needs attention': 'warn', Good: 'ok', Information: 'info' };
const FIELDS = [['name', 'Computer name', true], ['ip', 'IP address'], ['os', 'Operating system'], ['seen', 'Last report / check-in time'], ['type', 'Device type']];
const PRIORITY = { High: 'bad', Medium: 'warn', Low: 'mute' };

export function mountInventoryMatch(root) {
  let dead = false, meta = null, staged = null, result = null;
  const body = h('div', { class: 'grid' });
  const tools = h('div', { class: 'btn-row' });
  root.append(h('div', { class: 'page' }, pageHead('Inventory match', 'Upload a report or inventory received from the centre. Each machine in the asset register is checked by Asset (CI) = computer name: found means the tool is installed, missing means it is not.', tools), body));

  const tool = h('input', { id: 'mt-tool', type: 'text', value: 'BigFix', maxlength: 40 });
  const prefix = h('input', { id: 'mt-prefix', type: 'text', value: 'ANK', maxlength: 40 });
  const target = h('input', { id: 'mt-target', type: 'number', value: 95, min: 1, max: 100 });
  const notify = h('input', { id: 'mt-notify', type: 'checkbox' });
  const file = h('input', { id: 'mt-file', type: 'file', accept: '.xlsx,.xlsm,.csv' });
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
        frow('mt-file', 'File from the centre (.xlsx or .csv)', file, 'Used exactly as received. It is only read; nothing is loaded into the register.'),
        frow('mt-tool', 'Name of the tool', tool, 'Shown in the findings and files, e.g. BigFix, antivirus.'),
        frow('mt-prefix', 'Site prefix of the Asset (CI)', prefix, 'Only register machines starting with this are checked (several allowed: ANK, ANKA).'),
        h('div', { class: 'frow' }, h('span', { class: 'flabel' }, 'Asset classes to check'), chips, h('div', { class: 'hint' }, 'Printers, switches and UPS units cannot run an agent, so they are off by default.')),
        frow('mt-target', 'Target coverage (%)', target),
        h('div', { class: 'frow' }, h('label', { class: 'opt', for: 'mt-notify' }, notify, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, 'E-mail each engineer their own list when the match finishes')),
          h('div', { class: 'hint' }, 'Each engineer gets only the machines assigned to them, with an Excel file. Leave it off to look first; you can send from the results page.')),
        h('button', { class: 'btn primary', type: 'button', onClick: doUpload }, icon('upload'), 'Read the file'))),
      panel('2 · Check the columns and match', { cls: 'span-7' }, stepEl),
      h('div', { class: 'span-12 stack-v' }, out),
      panel('Earlier analyses', { cls: 'span-12', flush: true }, history()));
    drawStep();
  }

  async function doUpload() {
    if (!file.files[0]) { toast('Choose the file received from the centre.', 'bad'); return; }
    stepEl.replaceChildren(h('div', { class: 'loading-line' }, 'Reading the file…'));
    try { staged = await upload('/api/match/upload', file.files[0]); prefix.value = prefix.value || staged.prefixes[0]?.[0] || ''; drawStep(); } catch (e) { stepEl.replaceChildren(errorBlock(e)); }
  }

  function drawStep() {
    if (!staged) { stepEl.replaceChildren(h('p', { class: 'muted' }, 'Upload a file to start.')); return; }
    const sels = {};
    const rows = FIELDS.map(([k, label, req]) => {
      const sel = h('select', { id: 'mt-m-' + k }, h('option', { value: '' }, req ? 'Choose…' : '(not in this file)'), ...staged.headers.map((x) => h('option', { value: x, selected: staged.mapping[k] === x }, x)));
      sels[k] = sel;
      return frow('mt-m-' + k, label + (req ? ' *' : ''), sel);
    });
    const pre = staged.prefixes.length ? h('p', { class: 'hint' }, 'Most common name prefixes in the file: ', staged.prefixes.slice(0, 8).map(([p, n]) => `${p} (${int(n)})`).join(', '), '.') : null;
    stepEl.replaceChildren(h('p', null, h('strong', null, staged.filename), ` · ${int(staged.rows)} rows`), ...rows, pre,
      h('button', { class: 'btn primary', type: 'button', onClick: () => doRun(sels) }, icon('analytics'), 'Match against the register'));
  }

  async function doRun(sels) {
    const mapping = Object.fromEntries(Object.entries(sels).map(([k, s]) => [k, s.value]).filter(([, v]) => v));
    if (!mapping.name) { toast('Choose the computer name column.', 'bad'); return; }
    if (!classes.size) { toast('Choose at least one asset class.', 'bad'); return; }
    out.replaceChildren(h('div', { class: 'loading-line' }, 'Matching…'));
    try {
      result = await send('/api/match/run', { stage_id: staged.stage_id, mapping, params: { tool: tool.value, prefix: prefix.value, classes: [...classes], target: Number(target.value) || 95, notify: notify.checked } });
      showResult(); load2();
      if (result.engineer_mail) { toast(`E-mailed ${result.engineer_mail.plan.filter((x) => x.status === 'SENT').length} engineers`); engineerDialog(result.engineer_mail); } else if (result.engineer_mail_error) toast(result.engineer_mail_error, 'bad');
    } catch (e) { out.replaceChildren(errorBlock(e)); }
  }

  async function load2() { try { meta = { ...meta, ...(await get('/api/match/meta')) }; } catch { /* history is a convenience */ } }

  async function open(id) {
    out.replaceChildren(h('div', { class: 'loading-line' }, 'Opening…'));
    try { result = await get('/api/match/runs/' + id); showResult(); out.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (e) { out.replaceChildren(errorBlock(e)); }
  }

  const MAILSTATE = { READY: ['info', 'Ready to send'], SENT: ['ok', 'Sent'], FAILED: ['bad', 'Failed'], ALREADY_SENT: ['mute', 'Already sent'], NO_ADDRESS: ['warn', 'No e-mail address'], NO_ENGINEER: ['warn', 'No engineer in the register'], NOTHING_URGENT: ['mute', 'Nothing urgent'] };

  // Who would get what, then (after a click) send. `done` is a result already in hand (sent automatically after the match).
  async function engineerDialog(done) {
    let data = done;
    try { if (!data) data = await send('/api/match/engineers', { run_id: result.run_id, dry_run: true }); } catch (e) { toast(e.message, 'bad'); return; }
    const list = h('div', null);
    const draw = (d) => {
      const ready = d.plan.filter((x) => x.status === 'READY');
      list.replaceChildren(
        !d.mail_enabled ? h('p', { class: 'rec-note warn', role: 'alert' }, icon('warning--alt--filled'), 'E-mail is not switched on (Administration > E-mail). You can see who would be mailed, but nothing can be sent yet.') : null,
        d.plan.length ? table([['name', 'Engineer', null, (r) => titleCase(r.name)], ['email', 'Address', null, (r) => r.email || '—'], ['machines', 'Machines', 'right'], ['missing', 'No agent', 'right'], ['high', 'Deployed', 'right'], ['silent', 'Silent', 'right'],
          ['status', 'Status', null, (r) => h('span', { class: 'badge ' + (MAILSTATE[r.status] || MAILSTATE.READY)[0], title: r.error || '' }, (MAILSTATE[r.status] || MAILSTATE.READY)[1])]], d.plan, 200) : h('p', { class: 'muted' }, 'No engineer has anything to do.'),
        h('p', { class: 'hint' }, `${ready.length} engineer(s) will be mailed. Each message lists only that engineer's machines and carries their own Excel file. An engineer already mailed for this analysis is skipped, so pressing Send twice is safe.`));
      return ready.length && d.mail_enabled;
    };
    const can = draw(data);
    openModal({ title: 'E-mail each engineer their own list', lead: 'Nothing is sent until you press Send.', body: list,
      actions: [{ label: 'Close' }, ...(can ? [{ label: 'Send now', primary: true, onClick: async () => {
        try { const r = await send('/api/match/engineers', { run_id: result.run_id, dry_run: false }); toast(`Sent to ${r.plan.filter((x) => x.status === 'SENT').length} engineers` + (r.plan.some((x) => x.status === 'FAILED') ? '; some failed - see the list' : '')); draw(r); return false; } catch (e) { toast(e.message, 'bad'); return false; }
      } }] : [])] });
  }

  const dl = (fmt) => async () => { try { await download('/api/match/export', { run_id: result.run_id, format: fmt }, 'coverage.' + fmt); } catch (e) { toast(e.message, 'bad'); } };

  const table = (cols, rows, limit = 100) => (rows.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
    h('thead', null, h('tr', null, cols.map((c) => h('th', { class: c[2] || null }, c[1])))),
    h('tbody', null, rows.slice(0, limit).map((r) => h('tr', null, cols.map(([k, , cls, fn]) => h('td', { class: cls || null }, fn ? fn(r) : (r[k] ?? '')))))),
  ), rows.length > limit ? h('div', { class: 'hint', style: { padding: '8px 12px' } }, `Showing the first ${int(limit)} of ${int(rows.length)}. The Excel file has them all.`) : null)
    : h('div', { class: 'muted', style: { padding: '16px' } }, 'Nothing here.'));

  const prio = (r) => (r.priority ? h('span', { class: 'badge ' + PRIORITY[r.priority] }, r.priority) : '');
  const covCell = (r) => h('span', { class: r.coverage >= result.summary.target ? 'ok' : r.coverage >= result.summary.target - 15 ? 'warn' : 'bad' }, pct(r.coverage));
  const group = (label, rows) => table([['label', label], ['total', 'Machines', 'right'], ['installed', 'Installed', 'right'], ['missing', 'Not installed', 'right'], ['high', 'Deployed, no agent', 'right'], ['coverage', 'Coverage', 'right', covCell]],
    rows.map((r) => ({ ...r, label: label === 'Class' || label === 'Engineer' ? titleCase(r.label) : r.label })), 60);

  function showResult() {
    const s = result.summary, t = s.tool;
    tools.replaceChildren(h('button', { class: 'btn', type: 'button', onClick: () => engineerDialog(null) }, icon('email'), 'E-mail engineers…'), h('button', { class: 'btn', type: 'button', onClick: dl('xlsx') }, icon('document--export'), 'Excel for the team'), h('button', { class: 'btn primary', type: 'button', onClick: dl('pdf') }, icon('document--pdf'), 'PDF for management'));
    const tone = s.rag === 'green' ? 'ok' : s.rag === 'amber' ? 'warn' : 'bad';
    const missing = result.assets.filter((r) => !r.installed).sort((a, b) => ['High', 'Medium', 'Low'].indexOf(a.priority) - ['High', 'Medium', 'Low'].indexOf(b.priority));
    const quiet = result.assets.filter((r) => r.reporting === 'Stale' || r.reporting === 'Dormant').sort((a, b) => b.r_age - a.r_age);
    const tabs = [
      ['Not installed', missing.length, () => table([['ci', 'Asset (CI)'], ['class', 'Class', null, (r) => titleCase(r.class)], ['model', 'Model'], ['status', 'Status', null, (r) => titleCase(r.status.replace(/_/g, ' '))], ['user', 'User', null, (r) => titleCase(r.user)], ['location', 'Location'],
        ['engineer', 'Engineer', null, (r) => titleCase(r.engineer)], ['priority', 'Priority', null, prio], ['action', 'Action', 'wrap wide']], missing)],
      ['Installed, not reporting', quiet.length, () => table([['ci', 'Asset (CI)'], ['class', 'Class', null, (r) => titleCase(r.class)], ['user', 'User', null, (r) => titleCase(r.user)], ['location', 'Location'], ['r_seen', 'Last report'], ['r_age', 'Days silent', 'right'], ['action', 'Action', 'wrap wide']], quiet)],
      ['Name differs', missing.filter((r) => r.possible).length, () => table([['ci', 'Asset (CI)'], ['possible', 'Seen in report as'], ['possible_by', 'Matched by'], ['action', 'Action', 'wrap wide']], missing.filter((r) => r.possible))],
      ['In report, not in register', result.unregistered.length, () => table([['name', 'Name in report'], ['ip', 'IP'], ['os', 'OS'], ['seen', 'Last report'], ['why', 'Finding', 'wrap wide'], ['action', 'Action', 'wrap wide']], result.unregistered)],
    ];
    const pane = h('div', null);
    const bar = h('div', { class: 'mtabs', role: 'tablist' });
    const pick = (i) => { [...bar.children].forEach((b, j) => b.setAttribute('aria-selected', String(i === j))); pane.replaceChildren(tabs[i][2]()); };
    tabs.forEach(([name, n], i) => bar.append(h('button', { class: 'mtab', type: 'button', role: 'tab', onClick: () => pick(i) }, name, h('span', { class: 'count' }, int(n)))));

    out.replaceChildren(
      h('div', { class: 'rec-note ' + tone, role: 'status' }, icon(tone === 'ok' ? 'checkmark--filled' : 'warning--filled'),
        `${t} is installed on ${int(s.installed)} of ${int(s.assets)} machines (${pct(s.coverage)}) against a target of ${pct(s.target)}. ${int(s.missing)} do not have it.`),
      kpiStrip([{ id: 'a', label: 'Machines checked', value: int(s.assets), sub: s.classes.map(titleCase).join(', ') }, { id: 'i', label: `${t} installed`, value: int(s.installed), tone: 'ok' },
        { id: 'm', label: 'Not installed', value: int(s.missing), tone: s.missing ? 'bad' : 'ok', sub: `${int(s.missing_high)} deployed to users` }, { id: 'c', label: 'Coverage', value: pct(s.coverage), tone, sub: `target ${pct(s.target)}` },
        ...(s.has_seen ? [{ id: 'd', label: 'Installed but silent', value: int(s.stale + s.dormant), tone: s.dormant ? 'warn' : null, sub: `${int(s.dormant)} over ${s.stale_days} days` }] : []),
        { id: 'u', label: 'Not in register', value: int(s.unregistered), tone: s.unregistered ? 'warn' : null, sub: 'report names with no CI' }]),
      h('div', { class: 'grid' },
        panel('Findings and what to do', { cls: 'span-12', flush: true }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, h('th', null, 'Rating'), h('th', null, 'Finding'), h('th', null, 'What to do'))),
          h('tbody', null, s.findings.map((f) => h('tr', null, h('td', null, h('span', { class: 'badge ' + (SEV[f.severity] || 'mute') }, f.severity)), h('td', { class: 'wrap' }, f.finding), h('td', { class: 'wrap' }, f.action))))))),
        panel('By class', { cls: 'span-6', flush: true }, group('Class', s.by_class)), panel('By engineer', { cls: 'span-6', flush: true, hint: 'who has machines to fix' }, group('Engineer', s.by_engineer)),
        panel('By location', { cls: 'span-6', flush: true }, group('Location', s.by_location)),
        panel('Models with most gaps', { cls: 'span-3' }, barRows(s.gap_models.map((m) => ({ label: m.label, n: m.n })), { tone: 'bad' })),
        s.os_mix.length ? panel('Operating systems (installed)', { cls: 'span-3' }, barRows(s.os_mix)) : null,
        panel('Detail', { cls: 'span-12', flush: true, hint: 'the Excel file holds every row' }, bar, pane)));
    pick(0);
  }

  function history() {
    if (!meta.history.length) return h('div', { class: 'muted', style: { padding: '16px' } }, 'No analyses yet.');
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['When', 'Tool', 'File', 'Machines', 'Installed', 'Coverage', 'By', ''].map((x) => h('th', null, x)))),
      h('tbody', null, meta.history.map((r) => h('tr', null, h('td', { class: 'nowrap' }, when(r.at)), h('td', null, r.tool), h('td', { class: 'wrap' }, r.filename), h('td', { class: 'right' }, int(+r.assets)), h('td', { class: 'right' }, int(+r.installed)),
        h('td', { class: 'right' }, pct(+r.coverage)), h('td', null, r.username), h('td', null, h('button', { class: 'link-btn', type: 'button', onClick: () => open(r.run_id) }, 'Open')))))));
  }

  load();
  return { destroy() { dead = true; }, onLive: () => {} };
}
