// Schema-driven edit form. Controls follow the field kind (text, choice, date, IP, CPF, engineer, linked record); choices with up to four
// options use icon radios, longer lists use a select. Server validation messages are shown inline against the field they belong to.
import { get } from '../core/api.js';
import { debounce, h, icon } from '../core/dom.js';
import { labelOf } from './badge.js';
import { person } from '../core/format.js';

let seq = 0;

export function buildForm({ fields, values = {}, engineers = [], create = false, onInput }) {
  const el = h('form', { class: 'eform', novalidate: true, autocomplete: 'off' });
  el.addEventListener('submit', (e) => e.preventDefault());
  const items = new Map();
  const groups = new Map();
  const list = fields.filter((f) => create || !f.create_only);

  for (const f of list) {
    if (!groups.has(f.group)) { const box = h('div', { class: 'fgroup' }, h('h3', null, f.group)); groups.set(f.group, box); el.append(box); }
    const id = 'f' + (++seq);
    const initial = values[f.key] == null ? '' : String(values[f.key]);
    const c = control(f, id, initial, engineers);
    const err = h('div', { class: 'ferr', id: id + '-e', role: 'alert', hidden: true });
    const label = h('label', { class: 'flabel', for: c.labelFor || id }, f.label, f.required ? h('span', { class: 'req', title: 'Required' }, ' *') : null);
    const row = h('div', { class: 'frow', 'data-kind': f.kind }, label, c.node, err);
    groups.get(f.group).append(row);
    c.node.addEventListener('input', () => { clearError(f.key); onInput?.(); });
    c.node.addEventListener('change', () => { clearError(f.key); onInput?.(); });
    items.set(f.key, { f, c, err, row, initial });
  }

  // suggestion lists: an independent one (e.g. Make) loads once; a dependent one (e.g. Model depends_on Make) reloads whenever
  // the field it depends on changes, filtered to that value
  for (const it of items.values()) {
    if (!it.c._load) continue;
    const dep = it.f.depends_on && items.get(it.f.depends_on);
    if (!dep) { it.c._load(''); continue; }
    const refresh = () => it.c._load(dep.c.read());
    let last = dep.c.read();
    dep.c.node.addEventListener('input', refresh);
    dep.c.node.addEventListener('change', () => {
      // a real change to the field this one depends on (e.g. Class -> Type): the old value no longer belongs, so clear it and offer a fresh list
      if (dep.c.read() !== last) {
        last = dep.c.read();
        if (it.c.read() !== '') { it.c.write(''); it.c.input?.dispatchEvent(new Event('input', { bubbles: true })); it.c.input?.dispatchEvent(new Event('change', { bubbles: true })); }
      }
      refresh();
    });
    refresh();
  }
  // a new asset's Asset (CI) defaults to its hostname, until the person types their own Asset (CI) - hostname is usually known
  // first and the CI number just needs to match it, but an org-convention CI number can still be typed directly to override
  if (create && items.has('asset_key') && items.has('hostname')) {
    const key = items.get('asset_key'), host = items.get('hostname');
    let keyTouched = !!key.initial;
    key.c.node.addEventListener('input', () => { keyTouched = true; });
    host.c.node.addEventListener('input', () => { if (!keyTouched) key.c.write(host.c.read()); });
  }
  // Editing an existing asset whose Hostname was never recorded (true for most of them): default the field to the asset's own
  // CI number rather than leaving it blank to type from scratch. A plain save then fills it in for real, a natural backfill one
  // edit at a time instead of a one-off bulk update. Only fires once, at form open - unlike the create-mode listener above,
  // the CI here is already fixed, so there is nothing to keep tracking as the person types.
  if (!create && items.has('hostname') && !values.hostname && values.asset_key) {
    items.get('hostname').c.write(values.asset_key);
  }
  // a new asset's Type suggests its Class, when every existing asset of that Type happens to share one - e.g. typing "LAPTOP"
  // fills Class = LAPTOP. Left alone (never overwritten once the person touches Class themselves) when it's ambiguous or unknown.
  if (create && items.has('asset_type') && items.has('asset_class') && items.get('asset_type').c.input) {
    const type = items.get('asset_type'), cls = items.get('asset_class');
    let classTouched = !!cls.initial;
    cls.c.node.addEventListener('input', () => { classTouched = true; });
    type.c.input.addEventListener('blur', async () => {
      const v = type.c.read();
      if (!v || classTouched) return;
      try {
        const r = await get('/api/edit/assets/lookup', { field: 'asset_class', filter: v });
        if (r.values.length === 1) cls.c.write(r.values[0]);
      } catch (_) { /* suggestion only */ }
    });
  }
  // choosing a Class fills Type when there is no real choice to make: exactly one Type is already used for that Class (ROUTER ->
  // ROUTER, PRINTER -> PRINTER), or none yet, in which case the Type is the Class name itself. Classes with several Types are left
  // for the person to pick from the fresh list.
  if (create && items.has('asset_class') && items.has('asset_type') && items.get('asset_type').c.input) {
    const cls = items.get('asset_class'), type = items.get('asset_type');
    cls.c.node.addEventListener('change', async () => {
      const c = cls.c.read();
      if (!c) return;
      let known = [];
      try { known = (await get('/api/edit/assets/lookup', { field: 'asset_type', filter: c })).values; } catch (_) { return; }
      if (cls.c.read() !== c || type.c.read() !== '' || known.length > 1) return;
      type.c.write(known[0] || c.replace(/_/g, ' '));
      type.c.input.dispatchEvent(new Event('input', { bubbles: true }));
      type.c.input.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }
  // a new asset's rate component and value follow its Type: the most common rate already used on assets of that Type is filled in,
  // that Type's other rate codes are offered as suggestions, and the value follows the component - none of it overwrites what the
  // person has typed themselves
  if (items.has('rate_component') && items.has('rate_value')) {
    const comp = items.get('rate_component'), val = items.get('rate_value'), type = items.get('asset_type');
    let compTouched = !!comp.initial, valTouched = !!val.initial, rates = [];
    const fetchRates = async (params) => { try { return (await get('/api/edit/assets/rates', params)).rates; } catch (_) { return []; } };
    val.c.node.addEventListener('input', () => { valTouched = true; });
    comp.c.node.addEventListener('input', () => { compTouched = true; });
    comp.c.node.addEventListener('change', async () => {
      const code = comp.c.read().toUpperCase();
      if (!code || valTouched) return;
      const hit = rates.find((r) => r.component === code) || (await fetchRates({ component: code }))[0];
      if (hit && hit.value != null) val.c.write(String(hit.value));
    });
    if (create && type?.c.input) {
      type.c.input.addEventListener('change', async () => {
        const t = type.c.read();
        rates = t ? await fetchRates({ type: t }) : [];
        comp.c.setOptions(rates.map((r) => r.component));
        if (compTouched) return;
        if (!rates.length) { comp.c.write(''); if (!valTouched) val.c.write(''); return; }      // no rate for this Type: drop the one filled for the previous Type
        comp.c.write(rates[0].component);
        if (!valTouched && rates[0].value != null) val.c.write(String(rates[0].value));
      });
    } else {
      fetchRates({}).then((all) => { rates = all; comp.c.setOptions(all.map((r) => r.component)); });
    }
  }
  // a field with fill_from_asset (e.g. a new call's Asset (CI)) fills the user and engineer straight from that asset's current record
  for (const it of items.values()) {
    if (!create || !it.f.fill_from_asset || !it.c.input) continue;
    it.c.input.addEventListener('blur', async () => {
      const key = it.c.read();
      if (!key) return;
      try {
        const r = await get(`/api/card/asset/${encodeURIComponent(key)}`);
        const row = r.row || {};
        const cpf = items.get('cpf_no');
        if (cpf && !cpf.c.read() && row.cpf_no) cpf.c.write(String(row.cpf_no));
        const eng = items.get('engineer');
        if (eng && !eng.c.read() && row.engineer_name) eng.c.write(row.engineer_name);
      } catch (_) { /* not a known asset yet, or the lookup failed - leave the other fields as they are */ }
    });
  }

  function clearError(key) {
    const it = items.get(key);
    if (!it) return;
    it.err.hidden = true; it.row.classList.remove('bad');
    it.c.input?.removeAttribute('aria-invalid');
  }

  return {
    el,
    read: (key) => items.get(key).c.read(),
    /** Fields whose value differs from what was loaded -> {field: value or null}. */
    changes() {
      const out = {};
      for (const [k, it] of items) {
        const v = it.c.read();
        if (v !== it.initial) out[k] = v === '' ? null : v;
      }
      return out;
    },
    /** Client-side completeness check for create mode; returns {field: message}. */
    missing() {
      const out = {};
      for (const [k, it] of items) if (it.f.required && it.c.read() === '') out[k] = 'Required';
      return out;
    },
    setErrors(map) {
      let first = null;
      for (const [k, msg] of Object.entries(map || {})) {
        const it = items.get(k);
        if (!it) continue;
        it.err.textContent = msg; it.err.hidden = false; it.row.classList.add('bad');
        it.c.input?.setAttribute('aria-invalid', 'true');
        it.c.input?.setAttribute('aria-describedby', it.err.id);
        first ||= it;
      }
      first?.row.scrollIntoView({ block: 'center' });
      first?.c.focus();
    },
    setValue(key, v) { items.get(key)?.c.write(v == null ? '' : String(v)); },
    focusFirst() { items.values().next().value?.c.focus(); },
  };
}

function control(f, id, initial, engineers) {
  const common = { id, 'aria-label': undefined };
  switch (f.kind) {
    case 'longtext': {
      const t = h('textarea', { ...common, rows: '3', maxlength: String(f.max || 500) });
      t.value = initial;
      return simple(t);
    }
    case 'date': {
      const i = h('input', { ...common, type: 'date', min: '2000-01-01' });
      i.value = initial;
      return simple(i);
    }
    case 'enum': if (f.lookup) return lookupSelect(f, id, initial); return f.required && f.values.length <= 4 ? radios(f, id, initial) : select(id, [['', '—'], ...f.values.map((v) => [v, f.raw_labels ? v : labelOf(v, f.key)])], initial);
    case 'engineer': return select(id, [['', '—'], ...engineers.map((k) => [k, person(k)])], initial);
    case 'ip': { const i = h('input', { ...common, type: 'text', inputmode: 'decimal', maxlength: '15', placeholder: '10.0.0.1', class: 'mono' }); i.value = initial; return simple(i); }
    case 'asset': case 'call': case 'cpf': return linked(f, id, initial);
    case 'number': { const i = h('input', { ...common, type: 'text', inputmode: 'decimal', maxlength: '14', class: 'mono' }); i.value = initial; return simple(i); }
    case 'suggest': return suggest(f, id, initial);
    default: {
      const i = h('input', { ...common, type: 'text', maxlength: String(f.max || 200), class: f.upper ? 'upper' : '' });
      i.value = initial;
      if (f.upper) i.addEventListener('blur', () => { i.value = i.value.trim().toUpperCase(); });
      return simple(i);
    }
  }
}

function simple(input) {
  return { node: input, input, read: () => input.value.trim(), write: (v) => { input.value = v; }, focus: () => input.focus() };
}

function select(id, options, initial) {
  const s = h('select', { id }, options.map(([v, t]) => h('option', { value: v }, t)));
  s.value = options.some(([v]) => v === initial) ? initial : (initial ? (s.append(h('option', { value: initial }, initial)), initial) : '');
  return simple(s);
}

/** Closed dropdown whose choices come from the server, for the engineer selected on the form (Location / Floor / Room): the unique
 *  values already used on that engineer's assets, ascending. `_load(filterValue)` re-fills it when the engineer changes. */
function lookupSelect(f, id, initial) {
  const ctrl = select(id, [['', '—']], initial);
  const s = ctrl.input;
  const fill = (vals) => {
    const keep = s.value;
    s.replaceChildren(h('option', { value: '' }, '—'), ...vals.map((v) => h('option', { value: v }, v)));
    if (keep && !vals.includes(keep)) s.append(h('option', { value: keep }, keep));
    s.value = keep;
  };
  const load = debounce(async (filterValue) => {
    try { fill((await get('/api/edit/assets/lookup', { field: f.lookup, filter: filterValue || '' })).values); } catch (_) { /* keep what is shown */ }
  }, 100);
  return { ...ctrl, _load: load };
}

/** Icon radio group: native radios stay in the DOM for keyboard and screen readers, the Carbon glyphs are what people see. */
function radios(f, id, initial) {
  const name = 'r' + id;
  const inputs = [];
  const wrap = h('div', { class: 'rgroup', role: 'radiogroup', id, 'aria-label': f.label }, f.values.map((v) => {
    const i = h('input', { type: 'radio', name, value: v });
    i.checked = v === initial;
    inputs.push(i);
    return h('label', { class: 'ropt' }, i, icon('radio-button', 'glyph off'), icon('radio-button--checked', 'glyph on'), h('span', null, labelOf(v, f.key)));
  }));
  return {
    node: wrap, input: null, labelFor: id,
    read: () => inputs.find((i) => i.checked)?.value || '',
    write: (v) => inputs.forEach((i) => { i.checked = i.value === v; }),
    focus: () => (inputs.find((i) => i.checked) || inputs[0]).focus(),
  };
}

const LINK_GROUP = { asset: 'assets', call: 'calls', cpf: 'employees' };
const LINK_PLACEHOLDER = { asset: 'CI number', call: 'SR ID', cpf: 'Type a name or CPF number' };

/** Text box with suggestions from the global search (asset CI numbers, SR IDs, or - for 'cpf' - an employee's name or CPF number;
 *  picking a name-based suggestion fills in their CPF number, which is what is actually stored). The server still verifies the record exists on save. */
function linked(f, id, initial) {
  const list = h('datalist', { id: id + '-l' });
  const i = h('input', { id, type: 'text', maxlength: '40', list: id + '-l', class: 'mono upper', placeholder: LINK_PLACEHOLDER[f.kind] });
  i.value = initial;
  const group = LINK_GROUP[f.kind];
  const look = debounce(async () => {
    const q = i.value.trim();
    if (q.length < 2) return;
    try {
      const r = await get('/api/search', { q });
      const g = r.groups.find((x) => x.dataset === group);
      list.replaceChildren(...(g ? g.items.map((it) => h('option', { value: it.id }, it.sub)) : []));
    } catch (_) { /* suggestions are optional */ }
  }, 180);
  i.addEventListener('input', look);
  i.addEventListener('blur', () => { i.value = i.value.trim().toUpperCase(); });
  const node = h('div', { class: 'linked' }, i, list);
  return { node, input: i, read: () => i.value.trim().toUpperCase(), write: (v) => { i.value = v; }, focus: () => i.focus() };
}

/** Text box with suggestions - always freely typable, since the suggestions are a helping hand, not a closed list. Three sources:
 *  a fixed list (`f.values`, e.g. uniform sizes - populated once, no server round trip), a fixed list PER value of the field it
 *  `depends_on` (`f.values_by`, e.g. Operating system editions per OS family - offline, no server round trip either, just a local
 *  swap), or the server (`f.lookup`, e.g. Model filtered by Make - existing asset data). `_load(filterValue)` is called by
 *  buildForm() to refresh a field's suggestions when the field it `depends_on` changes; a plain fixed-list field has no `_load`
 *  at all, since there is nothing to refresh. */
function suggest(f, id, initial) {
  const list = h('datalist', { id: id + '-l' });
  const i = h('input', { id, type: 'text', maxlength: String(f.max || 200), list: id + '-l', class: f.upper ? 'upper' : '' });
  i.value = initial;
  if (f.upper) i.addEventListener('blur', () => { i.value = i.value.trim().toUpperCase(); });
  // A datalist only offers options that match what is already typed, so a field that holds a value shows nothing but that value.
  // On entering the field the current value is moved into the placeholder so the whole list is offered; it comes back on leaving
  // unless something else was typed or picked.
  let prev = '';
  i.addEventListener('focus', () => { if (i.value) { prev = i.value; i.placeholder = prev; i.value = ''; } });
  i.addEventListener('input', () => { prev = ''; i.placeholder = ''; });
  i.addEventListener('blur', () => { if (i.value === '' && prev) i.value = prev; prev = ''; i.placeholder = ''; });
  const node = h('div', { class: 'linked' }, i, list);
  const ctrl = { node, input: i, read: () => i.value.trim(), write: (v) => { i.value = v; }, focus: () => i.focus(),
                 setOptions: (vals) => list.replaceChildren(...vals.map((v) => h('option', { value: v }))) };
  if (f.values_by) {
    return { ...ctrl, _load: (filterValue) => list.replaceChildren(...(f.values_by[filterValue] || []).map((v) => h('option', { value: v }))) };
  }
  if (!f.lookup) {
    list.replaceChildren(...(f.values || []).map((v) => h('option', { value: v })));
    return ctrl;
  }
  const load = debounce(async (filterValue) => {
    try {
      const r = await get('/api/edit/assets/lookup', { field: f.lookup, filter: filterValue || '' });
      list.replaceChildren(...r.values.map((v) => h('option', { value: v })));
    } catch (_) { /* suggestions are optional */ }
  }, 150);
  return { ...ctrl, _load: load };
}
