// Per-register column layout: which columns show, and in what order. Saved against the signed-in account (app/views_pref.py),
// so "set as default" and "reset to original view" follow the person to any machine they sign in on.
import { get, send } from '../core/api.js';
import { h, icon } from '../core/dom.js';
import { openModal } from './modal.js';

/** Merge the dataset's own column list with a saved layout ({key, visible} in the saved order). A column the dataset gained or
 *  lost since the view was saved is reconciled: new ones are appended (visible), removed ones are dropped silently. */
export function resolveColumns(allColumns, saved) {
  if (!saved || !saved.length) return allColumns.map((c) => ({ ...c, visible: true }));
  const byKey = new Map(allColumns.map((c) => [c.key, c]));
  const out = [];
  const seen = new Set();
  for (const s of saved) {
    const c = byKey.get(s.key);
    if (!c) continue;
    out.push({ ...c, visible: s.visible });
    seen.add(s.key);
  }
  for (const c of allColumns) if (!seen.has(c.key)) out.push({ ...c, visible: true });
  return out;
}

export async function loadColumns(name, allColumns) {
  try {
    const r = await get(`/api/views/${name}`);
    return resolveColumns(allColumns, r.columns);
  } catch (_) {
    return allColumns.map((c) => ({ ...c, visible: true }));
  }
}

/** columnsDialog({name, columns, onApply}): columns is the current resolved list ({key,label,visible,...}); onApply(nextColumnsOrNull)
 *  is called after Save (with the new resolved list) or Reset (with null, meaning "back to the dataset's own default order"). */
export function columnsDialog({ name, columns, onApply }) {
  let working = columns.map((c) => ({ ...c }));

  function move(i, dir) {
    const j = i + dir;
    if (j < 0 || j >= working.length) return;
    [working[i], working[j]] = [working[j], working[i]];
    draw();
  }

  const list = h('div', { class: 'col-list' });
  function draw() {
    list.replaceChildren(...working.map((c, i) => {
      const cb = h('input', { type: 'checkbox', id: 'cc-' + c.key });
      cb.checked = c.visible;
      cb.addEventListener('change', () => { c.visible = cb.checked; });
      const up = h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': `Move ${c.label} up`, disabled: i === 0 ? true : null, onClick: () => move(i, -1) }, icon('chevron--up', 'sm'));
      const down = h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': `Move ${c.label} down`, disabled: i === working.length - 1 ? true : null, onClick: () => move(i, 1) }, icon('chevron--down', 'sm'));
      return h('div', { class: 'col-row' },
        h('label', { class: 'opt', for: 'cc-' + c.key }, cb, icon('checkbox', 'glyph off'), icon('checkbox--checked--filled', 'glyph on'), h('span', { class: 'name' }, c.label)),
        h('div', { class: 'col-move' }, up, down));
    }));
  }
  draw();

  openModal({
    title: 'Columns', wide: true,
    lead: 'Choose which columns to show, and use the arrows to put them in the order you want.',
    body: list,
    actions: [
      { label: 'Reset to default', onClick: async () => { await send(`/api/views/${name}/reset`, {}); onApply(null); } },
      { label: 'Cancel' },
      { label: 'Save', primary: true, keepOpen: true, onClick: async (m) => {
          const visible = working.filter((c) => c.visible);
          if (!visible.length) { m.error('Keep at least one column visible.'); return false; }
          const r = await send(`/api/views/${name}/save`, { columns: working.map((c) => ({ key: c.key, visible: c.visible })) });
          onApply(resolveColumns(columns, r.columns));
          m.close();
        } },
    ],
  });
}
