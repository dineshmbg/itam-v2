// Small tables (dashboards, admin pages, report preview) already size their columns to their content. This adds the one rule the browser can't
// work out alone: a cell holding a long text (over WRAP_AT characters) wraps inside a capped width instead of stretching its column across the
// whole card, so every column is as wide as its data - up to a sensible limit - and the table scrolls sideways far less often.
const WRAP_AT = 36;
let queued = false;

function fit(root) {
  root.querySelectorAll('table.tbl td:not(.wrap):not([data-fit])').forEach((td) => {
    td.dataset.fit = '1';
    if (td.querySelector('.badge, input, select, button, .btn')) return;
    if (td.textContent.trim().length > WRAP_AT) td.classList.add('wrap');
  });
}

/** Watch a container (the main area) and fit every table that appears in it, including ones that fill in after a fetch. */
export function watchTables(root) {
  if (root._fitWatching) return;       // called on every route change; one observer per container is enough
  root._fitWatching = true;
  const run = () => { queued = false; fit(root); };
  new MutationObserver(() => { if (!queued) { queued = true; setTimeout(run, 0); } }).observe(root, { childList: true, subtree: true });
  fit(root);
}
