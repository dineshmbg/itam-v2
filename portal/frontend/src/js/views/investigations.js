// Investigations: a finished analysis (an Excel workbook, optionally with its PDF summary) kept in the portal. Each sheet is read on screen with the same
// layout it has in Excel (bold, fill colours, merged cells); the original files can be downloaded again. Administrators and Team Leaders/SI only.
import { downloadGet, get, send, upload } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { bytes, when } from '../core/format.js';
import { toast } from '../ui/toast.js';
import { openModal } from '../ui/modal.js';
import { errorBlock, loading, pageHead, panel } from './common.js';

const PAGE = 300;
const show = (v) => (v === null || v === undefined ? '' : typeof v === 'number' ? v.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : String(v));

/** Inline style for a cell from the stored [bold, fill, font colour]; a fill always comes with a dark font so it reads in the dark theme too. */
function cellStyle(s, r, c, v) {
  const st = s.styles[`${r}:${c}`];
  const o = typeof v === 'number' ? { textAlign: 'right' } : {};
  if (!st) return o;
  if (st[0]) o.fontWeight = '700';
  if (st[1]) { o.background = '#' + st[1]; o.color = st[2] ? '#' + st[2] : '#1f2328'; }
  return o;
}

export function mountInvestigations(root) {
  let dead = false, data = null, cur = null, tab = 0;
  const cache = new Map();
  const tools = h('div', { class: 'btn-row' });
  const body = h('div', { class: 'stack-v' });
  root.append(h('div', { class: 'page' }, pageHead('Investigations', 'Finished analyses kept in the portal: read every sheet here, or download the Excel and PDF as they were made. Nothing is ever erased; removing one only hides it from the current list.', tools), body));
  body.append(loading());

  async function load(pick) {
    try {
      data = await get('/api/investigations');
      if (dead) return;
      if (pick) cur = pick;
      if (!data.items.some((x) => x.inv_id === cur)) cur = (data.items.find((x) => !x.superseded_at) || data.items[0])?.inv_id ?? null;
      draw();
    } catch (e) { if (!dead) body.replaceChildren(errorBlock(e, () => load())); }
  }

  const item = () => data.items.find((x) => x.inv_id === cur);
  const dl = (which, name) => downloadGet(`/api/investigations/${cur}/file/${which}`, null, name).catch((e) => toast(e.message, 'bad'));

  function draw() {
    tools.replaceChildren(h('button', { class: 'btn primary', type: 'button', onClick: addDialog }, icon('upload'), 'Add an investigation'));
    const it = item();
    if (!it) { body.replaceChildren(h('div', { class: 'panel' }, h('div', { class: 'panel-b' }, h('p', { class: 'muted' }, 'No investigation has been added yet. Use Add an investigation to upload a finished Excel report.')))); return; }
    const sel = h('select', { id: 'iv-pick', 'aria-label': 'Investigation' }, data.items.map((x) => h('option', { value: x.inv_id, selected: x.inv_id === cur }, `${x.title} · ${when(x.uploaded_at)}${x.superseded_at ? ' (removed)' : ''}`)));
    sel.onchange = () => { cur = Number(sel.value); tab = 0; draw(); };
    const gone = !!it.superseded_at;
    const acts = h('div', { class: 'btn-row' },
      h('button', { class: 'btn', type: 'button', onClick: () => dl('xlsx', it.xlsx_name) }, icon('document--export'), `Excel (${bytes(it.xlsx_size)})`),
      it.pdf_name ? h('button', { class: 'btn', type: 'button', onClick: () => dl('pdf', it.pdf_name) }, icon('document--pdf'), `PDF (${bytes(it.pdf_size)})`)
        : (gone ? null : h('button', { class: 'btn', type: 'button', onClick: pdfDialog }, icon('add'), 'Add the PDF')),
      gone ? null : h('button', { class: 'btn ghost', type: 'button', onClick: removeDialog }, icon('trash-can'), 'Remove'));
    const bar = h('div', { class: 'mtabs inv-tabs', role: 'tablist' });
    it.sheet_index.forEach((s, i) => bar.append(h('button', { class: 'mtab', type: 'button', role: 'tab', 'aria-selected': String(i === tab), onClick: () => { tab = i; [...bar.children].forEach((b, j) => b.setAttribute('aria-selected', String(i === j))); sheet(i); } },
      s.name, s.kind === 'table' ? h('span', { class: 'count' }, s.rows.toLocaleString('en-IN')) : null)));
    const pane = h('div', { class: 'inv-pane' });
    body.replaceChildren(
      h('div', { class: 'inv-head' }, h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'iv-pick' }, 'Investigation'), sel), acts),
      panel(it.title, { hint: `added by ${it.uploaded_by}, ${when(it.uploaded_at)}${gone ? ` · removed by ${it.superseded_by}, ${when(it.superseded_at)}` : ''}`, flush: true },
        it.note ? h('p', { class: 'inv-note' }, it.note) : null, bar, pane));
    sheet(tab);

    async function sheet(i) {
      pane.replaceChildren(loading());
      const key = `${cur}:${i}`;
      try {
        if (!cache.has(key)) cache.set(key, await get(`/api/investigations/${cur}/sheet/${i}`));
        if (dead || !pane.isConnected) return;
        pane.replaceChildren(cache.get(key).kind === 'table' ? tableView(cache.get(key)) : docView(cache.get(key)));
      } catch (e) { if (!dead && pane.isConnected) pane.replaceChildren(errorBlock(e, () => sheet(i))); }
    }
  }

  /** A sheet that is one table (header row, many rows): searchable, shown 300 rows at a time. */
  function tableView(s) {
    const head = s.rows[s.header];
    const rows = s.rows.slice(s.header + 1).map((row, k) => ({ row, r: s.header + 1 + k, text: row.map(show).join(' \u0001 ').toLowerCase() }));
    let q = '', limit = PAGE;
    const tbody = h('tbody', null), count = h('span', { class: 'muted' }), more = h('button', { class: 'btn', type: 'button' }, 'Show more');
    const search = h('input', { type: 'search', placeholder: 'Search this sheet', 'aria-label': 'Search this sheet', class: 'inv-search' });
    const draw2 = () => {
      const hit = q ? rows.filter((x) => x.text.includes(q)) : rows;
      tbody.replaceChildren(...hit.slice(0, limit).map((x) => h('tr', null, head.map((_, c) => { const v = x.row[c]; return h('td', { style: cellStyle(s, x.r, c, v) }, show(v)); }))));
      count.textContent = `${Math.min(limit, hit.length).toLocaleString('en-IN')} of ${hit.length.toLocaleString('en-IN')} rows${q ? ` match (of ${rows.length.toLocaleString('en-IN')})` : ''}`;
      more.hidden = hit.length <= limit;
    };
    let t;
    search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { q = search.value.trim().toLowerCase(); limit = PAGE; draw2(); }, 160); });
    more.addEventListener('click', () => { limit += PAGE; draw2(); });
    draw2();
    const pre = s.rows.slice(0, s.header).map((r) => r.find((v) => v)).filter(Boolean);
    return h('div', { class: 'inv-tablewrap' },
      pre.length ? h('p', { class: 'inv-note' }, pre.join(' ')) : null,
      h('div', { class: 'inv-bar' }, search, count),
      h('div', { class: 'tbl-wrap inv-scroll' }, h('table', { class: 'tbl inv-tbl' }, h('thead', null, h('tr', null, head.map((v, c) => h('th', { scope: 'col', style: cellStyle(s, s.header, c, null) }, show(v))))), tbody)),
      h('div', { class: 'inv-more' }, more));
  }

  /** A sheet that is text and several small tables: laid out like Excel, with merged cells and text that overflows into empty neighbours. */
  function docView(s) {
    const skip = new Set(), span = new Map(), width = Math.max(...s.rows.map((r) => r.length), 1);
    for (const [r1, c1, r2, c2] of s.merges) {
      span.set(`${r1}:${c1}`, [r2 - r1 + 1, c2 - c1 + 1]);
      for (let r = r1; r <= r2; r++) for (let c = c1; c <= c2; c++) if (r !== r1 || c !== c1) skip.add(`${r}:${c}`);
    }
    const rows = s.rows.map((row, r) => {
      if (row.every((v, c) => (v === null || v === '') && !s.styles[`${r}:${c}`])) return h('tr', { class: 'inv-gap' }, h('td', { colspan: width }));
      return h('tr', null, row.map((v, c) => {
        if (skip.has(`${r}:${c}`)) return null;
        let [rs, cs] = span.get(`${r}:${c}`) || [1, 1];
        if (cs === 1 && typeof v === 'string' && v.length > 40) { while (c + cs < width && (row[c + cs] === null || row[c + cs] === '') && !skip.has(`${r}:${c + cs}`)) cs++; }   // like Excel: long text runs on over empty cells
        return h('td', { colspan: cs > 1 ? cs : null, rowspan: rs > 1 ? rs : null, style: cellStyle(s, r, c, v) }, show(v));
      }));
    });
    return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl inv-doc' }, h('tbody', null, rows)));
  }

  function addDialog() {
    const f = (id, label, el, hint) => h('div', { class: 'frow' }, h('label', { class: 'flabel', for: id }, label), el, hint ? h('div', { class: 'hint' }, hint) : null);
    const title = h('input', { id: 'iv-title', type: 'text', maxlength: '120', placeholder: 'e.g. Trend Micro coverage, 23 Oct 2026' });
    const note = h('textarea', { id: 'iv-note', rows: '3', maxlength: '1000', placeholder: 'What this investigation is about (optional)' });
    const xl = h('input', { id: 'iv-xlsx', type: 'file', accept: '.xlsx' });
    const pdf = h('input', { id: 'iv-pdf', type: 'file', accept: '.pdf,application/pdf' });
    openModal({
      title: 'Add an investigation', lead: `The Excel workbook is read sheet by sheet (up to ${data.max_xlsx_mb} MB). The PDF summary is optional.`, wide: true,
      body: h('div', null, f('iv-title', 'Title', title), f('iv-note', 'Note', note), f('iv-xlsx', 'Excel workbook (.xlsx)', xl, 'Formulas show their last calculated value. Hidden sheets are left out.'), f('iv-pdf', 'PDF summary (optional)', pdf)),
      actions: [{ label: 'Cancel' }, { label: 'Upload', primary: true, icon: 'upload', onClick: async () => {
        const x = xl.files[0], p = pdf.files[0];
        if (title.value.trim().length < 3) throw new Error('Give the investigation a title.');
        if (!x) throw new Error('Choose the Excel workbook.');
        if (x.size > data.max_xlsx_mb * 1048576) throw new Error(`The workbook is larger than ${data.max_xlsx_mb} MB.`);
        if (p && p.size > data.max_pdf_mb * 1048576) throw new Error(`The PDF is larger than ${data.max_pdf_mb} MB.`);
        const r = await upload('/api/investigations/upload', x, { 'X-Title': encodeURIComponent(title.value.trim()), 'X-Note': encodeURIComponent(note.value.trim()) });
        if (p) { try { await upload(`/api/investigations/${r.inv_id}/pdf`, p); } catch (e) { toast(`Saved, but the PDF was not added: ${e.message}`, 'bad'); } }
        toast('Investigation saved');
        tab = 0;
        await load(r.inv_id);
      } }],
    });
  }

  function pdfDialog() {
    const pdf = h('input', { id: 'iv-pdf2', type: 'file', accept: '.pdf,application/pdf' });
    openModal({
      title: 'Add the PDF summary', lead: 'A PDF can be added once. It is never overwritten; to change it, add a new investigation.',
      body: h('div', { class: 'frow' }, h('label', { class: 'flabel', for: 'iv-pdf2' }, 'PDF file'), pdf),
      actions: [{ label: 'Cancel' }, { label: 'Add', primary: true, icon: 'upload', onClick: async () => {
        const p = pdf.files[0];
        if (!p) throw new Error('Choose the PDF file.');
        if (p.size > data.max_pdf_mb * 1048576) throw new Error(`The PDF is larger than ${data.max_pdf_mb} MB.`);
        await upload(`/api/investigations/${cur}/pdf`, p);
        toast('PDF added');
        await load();
      } }],
    });
  }

  function removeDialog() {
    openModal({
      title: `Remove ${item().title}`, lead: 'It is taken off the current list. It is not erased: the sheets and both files stay, and it can still be opened from the list above.',
      actions: [{ label: 'Cancel' }, { label: 'Remove', danger: true, onClick: async () => { await send('/api/investigations/remove', { inv_id: cur }); toast('Investigation removed'); await load(); } }],
    });
  }

  load();
  return { destroy() { dead = true; }, onLive: () => {} };
}
