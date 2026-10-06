import { token } from '../core/theme.js';
import { int } from '../core/format.js';

let lib;
const live = new Set();

export async function Chart() {
  if (!lib) lib = (await import('./chart-setup.js')).Chart;
  return lib;
}

const U = (a) => a.map((x) => String(x).toUpperCase());   // text rule: chart labels are upper case like the rest of the page
const PALETTE = ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6', '--chart-7', '--chart-8', '--chart-9', '--chart-10'];
export const color = (i) => token(PALETTE[i % PALETTE.length]);
export const colorVar = (i) => `var(${PALETTE[i % PALETTE.length]})`;

// Colours are scriptable so a theme change only needs chart.update('none').
const fontFamily = () => getComputedStyle(document.body).fontFamily;
function baseOptions(extra = {}) {
  return {
    responsive: true, maintainAspectRatio: false, animation: { duration: 450, easing: 'easeOutCubic', delay: (ctx) => (ctx.type === 'data' && ctx.mode === 'default' ? Math.min(ctx.dataIndex * 12, 120) : 0) }, layout: { padding: 0 },
    interaction: { mode: 'nearest', intersect: true },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: () => token('--c-surface'), titleColor: () => token('--c-text'), bodyColor: () => token('--c-text-2'), borderColor: () => token('--c-border-strong'), caretSize: 6, caretPadding: 10, titleMarginBottom: 6,
        borderWidth: 1, cornerRadius: 12, padding: 12, boxPadding: 5, boxWidth: 8, boxHeight: 8, usePointStyle: true, displayColors: true, titleFont: { family: fontFamily(), weight: '700', size: 12 }, bodyFont: { family: fontFamily(), size: 12 },
        callbacks: { label: (c) => ` ${c.dataset.label ? c.dataset.label + ': ' : ''}${int(c.parsed.x ?? c.parsed.y ?? c.parsed)}` },
      },
    },
    ...extra,
  };
}
const axis = (over = {}) => ({
  grid: { color: () => token('--chart-grid'), drawTicks: false, tickBorderDash: [3, 3] }, border: { display: false, dash: [3, 3] },
  ticks: { color: () => token('--c-text-2'), font: { family: fontFamily(), size: 12 }, padding: 6, precision: 0 }, ...over,
});

function track(chart) {
  live.add(chart);
  const destroy = chart.destroy.bind(chart);
  chart.destroy = () => { live.delete(chart); destroy(); };
  return chart;
}
export function destroyAll() { for (const c of live) c.destroy(); live.clear(); }
window.addEventListener('themechange', () => { for (const c of live) c.update('none'); });

/** Horizontal bar. datasets: [{label, data, tone?}] ; stacked optional. onPick(index) drills down. */
export async function barH(canvas, labels, datasets, { stacked = false, onPick, colors } = {}) {
  const C = await Chart();
  const chart = new C(canvas, {
    type: 'bar',
    data: {
      labels: U(labels),
      datasets: datasets.map((d, i) => ({
        label: String(d.label).toUpperCase(), data: d.data, borderWidth: 0, borderRadius: stacked ? 3 : 6, borderSkipped: false, barPercentage: 0.66, categoryPercentage: 0.9,
        backgroundColor: (ctx) => (colors ? color(colors[ctx.dataIndex]) : color(d.color ?? i)),
        hoverBackgroundColor: (ctx) => (colors ? color(colors[ctx.dataIndex]) : color(d.color ?? i)),
      })),
    },
    options: baseOptions({
      indexAxis: 'y',
      scales: { x: axis({ stacked, beginAtZero: true }), y: axis({ stacked, grid: { display: false }, ticks: { color: () => token('--c-text'), font: { family: fontFamily(), size: 12 }, padding: 8 } }) },
      onClick: (_e, els) => { if (onPick && els.length) onPick(els[0].index, els[0].datasetIndex); },
      onHover: (e, els) => { e.native.target.style.cursor = onPick && els.length ? 'pointer' : 'default'; },
    }),
  });
  return track(chart);
}

/** Vertical stacked bars with an optional line on a second axis (monthly volume + average TAT). */
export async function monthly(canvas, labels, bars, line, { onPick } = {}) {
  const C = await Chart();
  const datasets = bars.map((d, i) => ({
    type: 'bar', label: String(d.label).toUpperCase(), data: d.data, borderWidth: 0, borderRadius: 3, borderSkipped: false, stack: 's', order: 2, barPercentage: 0.62, categoryPercentage: 0.8,
    backgroundColor: () => color(d.color ?? i),
  }));
  if (line) {
    datasets.push({
      type: 'line', label: String(line.label).toUpperCase(), data: line.data, yAxisID: 'y1', order: 1, tension: 0.35, pointRadius: 3, pointHoverRadius: 6, borderWidth: 2.5, borderCapStyle: 'round',
      borderColor: () => token('--c-text'), backgroundColor: () => token('--c-surface'), pointBorderColor: () => token('--c-text'), pointBackgroundColor: () => token('--c-surface'), pointBorderWidth: 2,
    });
  }
  const chart = new C(canvas, {
    type: 'bar',
    data: { labels: U(labels), datasets },
    options: baseOptions({
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: axis({ stacked: true, grid: { display: false } }),
        y: axis({ stacked: true, beginAtZero: true }),
        y1: axis({ position: 'right', beginAtZero: true, grid: { display: false }, title: { display: false } }),
      },
      onClick: (_e, els) => { if (onPick && els.length) onPick(els[0].index); },
    }),
  });
  return track(chart);
}

// Centre read-out for doughnuts: the total, or the slice under the pointer.
const centreText = {
  id: 'centreText',
  afterDraw(chart) {
    const meta = chart.getDatasetMeta(0), arc = meta?.data?.[0];
    if (!arc) return;
    const { ctx } = chart, data = chart.data.datasets[0].data;
    const active = chart.getActiveElements()[0], i = active ? active.index : -1;
    const total = data.reduce((a, b) => a + (Number(b) || 0), 0);
    const value = i >= 0 ? Number(data[i]) || 0 : total;
    const label = i >= 0 ? String(chart.data.labels[i]) : 'TOTAL';
    const pct = i >= 0 && total ? ` · ${Math.round((100 * value) / total)}%` : '';
    const r = arc.innerRadius;
    if (r < 36) return;
    ctx.save(); ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillStyle = token('--c-text'); ctx.font = `600 ${Math.min(30, r * 0.46)}px ${fontFamily()}`;
    ctx.fillText(int(value), arc.x, arc.y - 6);
    ctx.fillStyle = token('--c-text-2'); ctx.font = `600 ${Math.max(10, Math.min(12, r * 0.18))}px ${fontFamily()}`;
    const txt = (label + pct).slice(0, Math.floor((r * 1.7) / 7));
    ctx.fillText(txt, arc.x, arc.y + Math.min(20, r * 0.32));
    ctx.restore();
  },
};

export async function doughnut(canvas, labels, data, { colors, onPick } = {}) {
  const C = await Chart();
  const chart = new C(canvas, {
    type: 'doughnut', plugins: [centreText],
    data: { labels: U(labels), datasets: [{ data, borderWidth: 3, borderRadius: 6, spacing: 2, borderColor: () => token('--c-surface'), backgroundColor: (ctx) => color(colors ? colors[ctx.dataIndex] : ctx.dataIndex), hoverOffset: 8, hoverBorderColor: () => token('--c-surface') }] },
    options: baseOptions({
      cutout: '72%',
      plugins: { ...baseOptions().plugins, tooltip: { ...baseOptions().plugins.tooltip, callbacks: { label: (c) => ` ${c.label}: ${int(c.parsed)}` } } },
      onClick: (_e, els) => { if (onPick && els.length) onPick(els[0].index); },
      onHover: (e, els) => { e.native.target.style.cursor = onPick && els.length ? 'pointer' : 'default'; },
    }),
  });
  return track(chart);
}

/** Update an existing chart in place (used by live refresh - no re-creation, no flicker). */
export function refresh(chart, labels, seriesData) {
  chart.data.labels = U(labels);
  seriesData.forEach((d, i) => { if (chart.data.datasets[i]) chart.data.datasets[i].data = d; });
  chart.update();
}
