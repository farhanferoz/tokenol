// Breakdown page entry point. Loaded as an ES module by breakdown.html.
//
// Responsibilities (added across PR1):
//  - Period pill state (sessionStorage, independent of Overview)
//  - Scorecard fetch + render
//  - Chart.js global defaults (from CSS design tokens)
//  - Two Time-section charts
//  - SSE-driven refresh

import { readCssVar } from './chart.js';
import { renderRankedBars } from './components.js';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const SS_PERIOD = 'tokenol.breakdown.period';
const VALID_RANGES = new Set(['7d', '30d', '90d', 'all']);

function getPeriod() {
  const v = sessionStorage.getItem(SS_PERIOD);
  return VALID_RANGES.has(v) ? v : '30d';
}
function setPeriod(p) { sessionStorage.setItem(SS_PERIOD, p); }

const _LS_BD_TIME_UNIT    = 'tokenol.breakdown.timeUnit';
const _LS_BD_PROJECT_UNIT = 'tokenol.breakdown.projectUnit';
const _LS_BD_MODEL_UNIT   = 'tokenol.breakdown.modelUnit';
const _LS_BD_TOOL_UNIT    = 'tokenol.breakdown.toolUnit';

let _bdTimeUnit    = localStorage.getItem(_LS_BD_TIME_UNIT)    || 'tokens';
let _bdProjectUnit = localStorage.getItem(_LS_BD_PROJECT_UNIT) || 'tokens';
let _bdModelUnit   = localStorage.getItem(_LS_BD_MODEL_UNIT)   || 'tokens';
let _bdToolUnit    = localStorage.getItem(_LS_BD_TOOL_UNIT)    || 'cost';

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

function fmtInt(n) {
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US');
}

function fmtTok(n) {
  if (!Number.isFinite(n)) return '—';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

function fmtUSD(n) {
  if (!Number.isFinite(n)) return '—';
  if (Math.abs(n) >= 1000) return `$${(n).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
  return `$${n.toFixed(2)}`;
}

function fmtPct(n, decimals = 1) {
  if (!Number.isFinite(n)) return '—';
  return `${n.toFixed(decimals)}%`;
}

// ---------------------------------------------------------------------------
// Summary / scorecard
// ---------------------------------------------------------------------------

async function fetchSummary(range) {
  const resp = await fetch(`/api/breakdown/summary?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`summary ${resp.status}`);
  return resp.json();
}

function renderScorecard(data) {
  document.getElementById('sc-activity-primary').innerHTML =
    `${fmtInt(data.sessions)} <span class="sc-unit">sessions</span>`;
  document.getElementById('sc-activity-sub').textContent =
    `${fmtInt(data.turns)} turns`;

  const billable = data.input_tokens + data.output_tokens;
  document.getElementById('sc-tokens-primary').textContent = fmtTok(billable);
  document.getElementById('sc-tokens-sub').textContent =
    `${fmtTok(data.input_tokens)} in · ${fmtTok(data.output_tokens)} out`;

  document.getElementById('sc-cache-primary').innerHTML =
    `${fmtTok(data.cache_read_tokens)} <span class="sc-unit">read</span>`;
  document.getElementById('sc-cache-sub').textContent =
    `${fmtTok(data.cache_creation_tokens)} created`;

  document.getElementById('sc-cost-primary').textContent = fmtUSD(data.cost_usd);
  document.getElementById('sc-cost-sub').textContent =
    data.cache_saved_usd > 0
      ? `cache saved ≈ ${fmtUSD(data.cache_saved_usd)}`
      : '';

  window.__breakdownCacheSaved = data.cache_saved_usd;
}

// ---------------------------------------------------------------------------
// Chart.js configuration (run once)
// ---------------------------------------------------------------------------

// CSS-var reads are memoized — design tokens are static for the page lifetime,
// and these are hit several times per chart × every SSE tick.
const _cssVarCache = new Map();
function cssVar(name) {
  let v = _cssVarCache.get(name);
  if (v === undefined) { v = readCssVar(name); _cssVarCache.set(name, v); }
  return v;
}

// Tokenol dataset color cycle, semantic.
// 0 → amber (input, primary), 1 → alarm (output), 2 → green (cache),
// 3 → cool (model axis), 4 → mute, 5 → amber-dim.
const _PAL_NAMES = ['--amber', '--alarm', '--green', '--cool', '--mute', '--amber-dim'];
function tokenolPalette() {
  return _PAL_NAMES.map(cssVar);
}

// ---------------------------------------------------------------------------
// Cache-health thresholds (default from metrics/thresholds.DEFAULTS).
// Overridden by /api/prefs on page load via loadThresholdsFromPrefs().
// Mutable module-level so renderers see the latest value on SSE tick
// without re-plumbing.
// ---------------------------------------------------------------------------

// Thresholds default to DEFAULTS from metrics/thresholds.py; overridden by
// /api/prefs on page load. Mutable module-level so renderers see the latest
// value on SSE tick without re-plumbing.
let HIT_PCT_GREEN = 95.0;
let HIT_PCT_RED = 85.0;

function healthColorForHitRate(rate) {
  // `rate` is a decimal in [0, 1] or null/undefined.
  if (rate == null) return cssVar('--mute');
  const pct = rate * 100;
  if (pct >= HIT_PCT_GREEN) return cssVar('--green');
  if (pct >= HIT_PCT_RED) return cssVar('--amber');
  return cssVar('--alarm');
}

let _captionSignature = '';
function renderByProjectCaption() {
  const el = document.getElementById('bp-by-project-caption');
  if (!el) return;
  const sig = `${HIT_PCT_GREEN}|${HIT_PCT_RED}`;
  if (sig === _captionSignature) return;
  el.innerHTML =
    `<span>cache hit rate</span>` +
    `<span class="caption-swatch caption-swatch--green"></span><span>≥${HIT_PCT_GREEN}%</span>` +
    `<span class="caption-swatch caption-swatch--amber"></span><span>${HIT_PCT_RED}–${HIT_PCT_GREEN}%</span>` +
    `<span class="caption-swatch caption-swatch--alarm"></span><span>&lt;${HIT_PCT_RED}%</span>`;
  _captionSignature = sig;
}

// ---------------------------------------------------------------------------
// Cache-health dots plugin for Chart.js.
//
// Chart.js tick callbacks can only return strings, so we can't inject a dot
// into the tick label. Instead we register a per-chart plugin that draws an
// 8 px colored circle aligned to each x-tick, just below the axis baseline.
//
// Usage: register via `plugins: [cacheHealthDotsPlugin]` and pass
// `options.plugins.cacheHealthDots.colors = [...]` aligned to the chart's
// x-axis tick order.
// ---------------------------------------------------------------------------

const cacheHealthDotsPlugin = {
  id: 'cacheHealthDots',
  afterDatasetsDraw(chart) {
    const opts = chart.options.plugins && chart.options.plugins.cacheHealthDots;
    if (!opts || !Array.isArray(opts.colors)) return;
    const xScale = chart.scales.x;
    if (!xScale) return;
    const ctx = chart.ctx;
    // Rotated 45° labels sit between xScale.bottom and roughly xScale.bottom+30.
    // Dots are drawn in a dedicated band below that. Keep in sync with
    // `layout.padding.bottom` on the chart options.
    const y = xScale.bottom + 38;
    ctx.save();
    for (let i = 0; i < xScale.ticks.length; i++) {
      const color = opts.colors[i];
      if (!color) continue;
      const x = xScale.getPixelForTick(i);
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    }
    ctx.restore();
  },
};

// ---------------------------------------------------------------------------
// Breakdowns-section charts
// ---------------------------------------------------------------------------

async function fetchByProject(range) {
  const resp = await fetch(`/api/breakdown/by-project?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`by-project ${resp.status}`);
  return resp.json();
}

const BY_PROJECT_TOP_N = 10;

let _chartByProject = null;
let _byProjectData = null; // cached payload for unit-toggle re-renders

function renderByProject(data) {
  if (data) _byProjectData = data;
  const d = _byProjectData;
  if (!d) return;

  renderByProjectCaption();
  const pal = tokenolPalette();
  // Cap to a readable number of bars; tail is dropped (not collapsed) so the
  // chart stays legible. Subheading notes how many were shown vs. total.
  const projects = d.projects.slice(0, BY_PROJECT_TOP_N);
  const labels = projects.map(p => p.project);
  const dotColors = projects.map(p => healthColorForHitRate(p.cache_hit_rate));
  const cwdB64 = projects.map(p => p.cwd_b64);
  const hitRate = projects.map(p => p.cache_hit_rate);

  const useCost = _bdProjectUnit === 'cost';
  const tickFmt = useCost ? fmtUSD : fmtTok;
  // $ mode adds cache_read so stacked bars sum to per-project cost.
  const datasets = useCost
    ? [
        { label: 'input',         data: projects.map(p => p.input_cost),          backgroundColor: pal[0], stack: 'all' },
        { label: 'output',        data: projects.map(p => p.output_cost),         backgroundColor: pal[1], stack: 'all' },
        { label: 'cache created', data: projects.map(p => p.cache_creation_cost), backgroundColor: pal[2], stack: 'all' },
        { label: 'cache read',    data: projects.map(p => p.cache_read_cost),     backgroundColor: pal[5], stack: 'all' },
      ]
    : [
        { label: 'input',         data: projects.map(p => p.input),               backgroundColor: pal[0], stack: 'all' },
        { label: 'output',        data: projects.map(p => p.output),              backgroundColor: pal[1], stack: 'all' },
        { label: 'cache created', data: projects.map(p => p.cache_creation),      backgroundColor: pal[2], stack: 'all' },
      ];

  // Caption always uses token counts regardless of mode (share metric).
  const shownTotal = projects.reduce((s, p) => s + p.input + p.output, 0);
  const allTotal = d.projects.reduce((s, p) => s + p.input + p.output, 0);
  const subEl = document.getElementById('bp-by-project-sub');
  if (d.projects.length > BY_PROJECT_TOP_N) {
    const pct = Math.round((shownTotal / Math.max(allTotal, 1)) * 100);
    subEl.textContent = `top ${BY_PROJECT_TOP_N} of ${d.projects.length} · ${pct}% of billable`;
  } else {
    subEl.textContent = `${d.projects.length} project${d.projects.length === 1 ? '' : 's'}`;
  }

  const canvas = document.getElementById('chart-by-project');

  if (_chartByProject) {
    _chartByProject.$tokenol = { cwdB64, hitRate };
    _chartByProject.data.labels = labels;
    _chartByProject.data.datasets = datasets;
    _chartByProject.options.plugins.cacheHealthDots.colors = dotColors;
    _chartByProject.options.scales.y.ticks.callback = tickFmt;
    _chartByProject.update('none');
    return;
  }

  _chartByProject = new window.Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    plugins: [cacheHealthDotsPlugin],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { bottom: 46 } },
      scales: {
        x: { stacked: true,
          ticks: { maxRotation: 45, minRotation: 45, autoSkip: false, padding: 4 },
        },
        y: { stacked: true, beginAtZero: true, ticks: { callback: tickFmt } },
      },
      plugins: {
        legend: { position: 'top', align: 'end' },
        cacheHealthDots: { colors: dotColors },
        tooltip: {
          callbacks: {
            afterBody(items) {
              if (!items.length) return '';
              const rate = items[0].chart.$tokenol.hitRate[items[0].dataIndex];
              return `cache hit rate: ${rate == null ? '—' : fmtPct(rate * 100)}`;
            },
          },
        },
      },
      onClick: (_evt, elements) => {
        if (!elements.length) return;
        const idx = elements[0].index;
        const b64 = _chartByProject.$tokenol.cwdB64[idx];
        if (b64) window.location.href = `/project/${b64}`;
      },
    },
  });
  _chartByProject.$tokenol = { cwdB64, hitRate };
}

async function fetchByModel(range) {
  const resp = await fetch(`/api/breakdown/by-model?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`by-model ${resp.status}`);
  return resp.json();
}

const BY_MODEL_TOP_N = 6;

let _chartByModel = null;
let _byModelData = null; // cached payload for unit-toggle re-renders

function collapseModels(models, useCost) {
  // Keep top N−1, collapse the tail into 'others' only if it would exceed N.
  // Colour assignment is based on position in the sorted-by-tokens list so it
  // stays consistent across TOKENS and $ modes.
  const valueOf = m => useCost ? m.cost_usd : (m.input + m.output);
  if (models.length <= BY_MODEL_TOP_N) {
    return models.map(m => ({ name: m.model, value: valueOf(m), isOthers: false }));
  }
  const head = models.slice(0, BY_MODEL_TOP_N - 1).map(m => ({
    name: m.model, value: valueOf(m), isOthers: false,
  }));
  const tailValue = models.slice(BY_MODEL_TOP_N - 1).reduce((s, m) => s + valueOf(m), 0);
  head.push({ name: 'others', value: tailValue, isOthers: true });
  return head;
}

function renderByModel(data) {
  if (data) _byModelData = data;
  const d = _byModelData;
  if (!d) return;

  const useCost = _bdModelUnit === 'cost';
  const pal = tokenolPalette();
  const collapsed = collapseModels(d.models, useCost);
  const labels = collapsed.map(c => c.name);
  const values = collapsed.map(c => c.value);
  // Colours are assigned by position in the original (tokens-sorted) order so
  // that the same model always gets the same colour regardless of active unit.
  const colors = collapsed.map((_, i) => pal[i % pal.length]);
  const names = collapsed.map(c => (c.isOthers ? null : c.name));

  document.getElementById('bp-by-model-sub').textContent =
    `${d.models.length} model${d.models.length === 1 ? '' : 's'}`;

  const canvas = document.getElementById('chart-by-model');

  const tooltipLabel = useCost
    ? function(ctx) {
        const total = ctx.dataset.data.reduce((s, v) => s + v, 0) || 1;
        const v = ctx.parsed;
        return `${ctx.label}: ${fmtUSD(v)} (${fmtPct((v / total) * 100)})`;
      }
    : function(ctx) {
        const total = ctx.dataset.data.reduce((s, v) => s + v, 0) || 1;
        const v = ctx.parsed;
        return `${ctx.label}: ${fmtTok(v)} billable (${fmtPct((v / total) * 100)})`;
      };

  if (_chartByModel) {
    _chartByModel.$tokenol = { names };
    _chartByModel.data.labels = labels;
    _chartByModel.data.datasets[0].data = values;
    _chartByModel.data.datasets[0].backgroundColor = colors;
    _chartByModel.options.plugins.tooltip.callbacks.label = tooltipLabel;
    _chartByModel.update('none');
    return;
  }

  _chartByModel = new window.Chart(canvas, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data: values, backgroundColor: colors, borderWidth: 1, borderColor: cssVar('--bg-raised') }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '60%',
      plugins: {
        legend: { position: 'bottom' },
        tooltip: {
          callbacks: {
            label: tooltipLabel,
          },
        },
      },
      onClick: (_evt, elements) => {
        if (!elements.length) return;
        const idx = elements[0].index;
        const name = _chartByModel.$tokenol.names[idx];
        if (name) window.location.href = `/model/${encodeURIComponent(name)}`;
      },
    },
  });
  _chartByModel.$tokenol = { names };
}

async function fetchTools(range) {
  const resp = await fetch(`/api/breakdown/tools?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`tools ${resp.status}`);
  return resp.json();
}

async function loadThresholdsFromPrefs() {
  try {
    const resp = await fetch('/api/prefs');
    if (!resp.ok) return;
    const prefs = await resp.json();
    const t = prefs.thresholds || {};
    if (Number.isFinite(t.hit_rate_good_pct)) HIT_PCT_GREEN = t.hit_rate_good_pct;
    if (Number.isFinite(t.hit_rate_red_pct))  HIT_PCT_RED   = t.hit_rate_red_pct;
  } catch (err) {
    console.warn('[breakdown] prefs fetch failed; using default thresholds', err);
  }
}

let _toolsData = null;

function renderToolMix(data) {
  if (data) _toolsData = data;
  const d = _toolsData;
  if (!d) return;
  const tools = d.tools || [];
  const useCost = _bdToolUnit === 'cost';

  // Filter the unattributed sentinel out of the count for the subheading, and
  // show "N tools · $X total" in cost mode, "N tools · Y calls" in tokens mode.
  const realTools = tools.filter(t => t.name !== '__unattributed__');
  const totalCalls = realTools.reduce((s, t) => s + (t.count || 0), 0);
  const totalCost = realTools.reduce((s, t) => s + (t.cost_usd || 0), 0);
  const subEl = document.getElementById('bp-tools-sub');
  if (realTools.length === 0) {
    subEl.textContent = 'no tool calls';
  } else {
    subEl.textContent = useCost
      ? `${realTools.length} tool${realTools.length === 1 ? '' : 's'} · ${fmtUSD(totalCost)} attributed`
      : `${realTools.length} tool${realTools.length === 1 ? '' : 's'} · ${fmtInt(totalCalls)} calls`;
  }

  const rows = tools.map(t => {
    let kind;
    if (t.name === 'other') kind = 'other';
    if (t.name === '__unattributed__') kind = 'unattributed';
    const displayName = t.name === '__unattributed__'
      ? 'unattributed'
      : (t.name === 'other' ? `other (${t.count || 0})` : t.name);
    const lastActiveDate = t.last_active ? t.last_active.slice(0, 10) : null;
    const callCount = t.count || 0;
    const sublabel = kind === 'unattributed'
      ? undefined
      : (lastActiveDate
        ? `${callCount} call${callCount === 1 ? '' : 's'} · ${lastActiveDate}`
        : `${callCount} call${callCount === 1 ? '' : 's'}`);
    return {
      label: displayName,
      sublabel,
      value: useCost ? (t.cost_usd || 0) : callCount,
      href: kind ? undefined : `/tool/${encodeURIComponent(t.name)}`,
      kind,
    };
  });
  const fmt = useCost ? fmtUSD : (n) => fmtInt(n) + ' calls';
  renderRankedBars(document.getElementById('bp-tools-bars'), rows, { valueFormat: fmt });
}

let _chartDefaultsApplied = false;
function configureChartDefaults() {
  if (_chartDefaultsApplied || typeof window.Chart === 'undefined') return;
  const Chart = window.Chart;
  Chart.defaults.font.family = "'JetBrains Mono', 'SF Mono', 'Courier New', monospace";
  Chart.defaults.font.size = 11;
  Chart.defaults.color = cssVar('--fg-2');
  Chart.defaults.borderColor = cssVar('--rule');
  Chart.defaults.plugins.tooltip.backgroundColor = cssVar('--bg-raised');
  Chart.defaults.plugins.tooltip.titleColor = cssVar('--fg');
  Chart.defaults.plugins.tooltip.bodyColor = cssVar('--fg-2');
  Chart.defaults.plugins.tooltip.borderColor = cssVar('--rule-2');
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.titleFont = { family: "'Instrument Serif', serif", size: 14 };
  Chart.defaults.plugins.legend.labels.color = cssVar('--fg-2');
  Chart.defaults.plugins.legend.labels.boxWidth = 10;
  Chart.defaults.plugins.legend.labels.boxHeight = 10;
  _chartDefaultsApplied = true;
}

// Chart.js is loaded as a deferred UMD script; it may not be ready when this
// module first evaluates. Poll briefly on a microtask until window.Chart shows up.
async function whenChartReady() {
  if (typeof window.Chart !== 'undefined') return window.Chart;
  for (let i = 0; i < 50; i++) {
    await new Promise(r => setTimeout(r, 40));
    if (typeof window.Chart !== 'undefined') return window.Chart;
  }
  throw new Error('Chart.js did not load within 2s');
}

// ---------------------------------------------------------------------------
// Time-section charts
// ---------------------------------------------------------------------------

async function fetchDailyTokens(range) {
  const resp = await fetch(`/api/breakdown/daily-tokens?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`daily-tokens ${resp.status}`);
  return resp.json();
}

let _chartDailyWork = null;
let _dailyWorkData = null; // cached payload for unit-toggle re-renders

function renderDailyWork(data) {
  if (data) _dailyWorkData = data;
  const d = _dailyWorkData;
  if (!d) return;

  const pal = tokenolPalette();
  const labels = d.days.map(day => day.date);

  const useCost = _bdTimeUnit === 'cost';
  // $ mode adds cache_read as a 4th component so the stacked bars sum to
  // cost_usd. Tokens mode keeps the existing 3-component shape — cache_read
  // tokens are a separate concept (Daily Cache Re-use chart) at a different
  // scale and would dominate this chart if included.
  const datasets = useCost
    ? [
        { label: 'input',         data: d.days.map(day => day.input_cost),          backgroundColor: pal[0], stack: 'all' },
        { label: 'output',        data: d.days.map(day => day.output_cost),         backgroundColor: pal[1], stack: 'all' },
        { label: 'cache created', data: d.days.map(day => day.cache_creation_cost), backgroundColor: pal[2], stack: 'all' },
        { label: 'cache read',    data: d.days.map(day => day.cache_read_cost),     backgroundColor: pal[5], stack: 'all' },
      ]
    : [
        { label: 'input',         data: d.days.map(day => day.input),               backgroundColor: pal[0], stack: 'all' },
        { label: 'output',        data: d.days.map(day => day.output),              backgroundColor: pal[1], stack: 'all' },
        { label: 'cache created', data: d.days.map(day => day.cache_creation),      backgroundColor: pal[2], stack: 'all' },
      ];

  const tickFmt = useCost ? fmtUSD : fmtTok;

  const totalCost = d.days.reduce((s, day) => s + day.cost_usd, 0);
  const days = Math.max(1, d.days.length);
  document.getElementById('bp-daily-work-sub').textContent =
    `total ${fmtUSD(totalCost)} · avg ${fmtUSD(totalCost / days)}/d`;

  const canvas = document.getElementById('chart-daily-work');
  if (_chartDailyWork) {
    _chartDailyWork.data.labels = labels;
    _chartDailyWork.data.datasets = datasets;
    _chartDailyWork.options.scales.y.ticks.callback = tickFmt;
    _chartDailyWork.update('none');
    return;
  }
  _chartDailyWork = new window.Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { stacked: true, ticks: { maxRotation: 45, minRotation: 45, autoSkip: true, maxTicksLimit: 14 } },
        y: { stacked: true, beginAtZero: true, ticks: { callback: tickFmt } },
      },
      plugins: { legend: { position: 'top', align: 'end' } },
    },
  });
}

let _chartDailyCache = null;

function renderDailyCache(data) {
  const pal = tokenolPalette();
  const labels = data.days.map(d => d.date);
  const datasets = [
    { label: 'cache read', data: data.days.map(d => d.cache_read), backgroundColor: pal[2] },
  ];

  // "Savings per day" subheading — pulled from the summary endpoint, not
  // daily-tokens, so this renderer reads it from the scorecard state.
  // For PR1 we compute a simple total-reads figure and a "avg $X/d saved" only
  // when the scorecard has already populated a cache_saved_usd number.
  const totalReads = data.days.reduce((s, d) => s + d.cache_read, 0);
  const days = Math.max(1, data.days.length);
  const savedTotal = window.__breakdownCacheSaved ?? 0;
  document.getElementById('bp-daily-cache-sub').textContent =
    savedTotal > 0
      ? `total ${fmtTok(totalReads)} · avg ${fmtUSD(savedTotal / days)}/d saved`
      : `total ${fmtTok(totalReads)}`;

  const canvas = document.getElementById('chart-daily-cache');
  if (_chartDailyCache) {
    // In-place update: avoids canvas destroy/recreate flicker on SSE tick.
    _chartDailyCache.data.labels = labels;
    _chartDailyCache.data.datasets[0].data = datasets[0].data;
    _chartDailyCache.data.datasets[0].backgroundColor = datasets[0].backgroundColor;
    _chartDailyCache.update('none');
    return;
  }
  _chartDailyCache = new window.Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { ticks: { maxRotation: 45, minRotation: 45, autoSkip: true, maxTicksLimit: 14 } },
        y: { beginAtZero: true, ticks: { callback: v => fmtTok(v) } },
      },
      plugins: { legend: { display: false } },
    },
  });
}

// ---------------------------------------------------------------------------
// Pill wiring
// ---------------------------------------------------------------------------

function _wireUnitPills(groupId, lsKey, getCurrent, setCurrent, onChange) {
  const group = document.getElementById(groupId);
  if (!group) return;
  // Sync initial DOM state to persisted value.
  group.querySelectorAll('[data-bdunit]').forEach(b => {
    b.classList.toggle('on', b.dataset.bdunit === getCurrent());
  });
  group.querySelectorAll('[data-bdunit]').forEach(btn => {
    btn.addEventListener('click', () => {
      const next = btn.dataset.bdunit;
      if (next === getCurrent()) return;
      setCurrent(next);
      localStorage.setItem(lsKey, next);
      group.querySelectorAll('[data-bdunit]').forEach(b =>
        b.classList.toggle('on', b.dataset.bdunit === next),
      );
      onChange(next);
    });
  });
}

function wirePeriodPills() {
  const group = document.getElementById('breakdown-period-pills');
  if (!group) return;
  // Sync initial highlight to stored value.
  const cur = getPeriod();
  for (const span of group.querySelectorAll('[data-range]')) {
    span.classList.toggle('on', span.dataset.range === cur);
    span.addEventListener('click', () => {
      const r = span.dataset.range;
      if (!VALID_RANGES.has(r)) return;
      setPeriod(r);
      for (const s of group.querySelectorAll('[data-range]')) {
        s.classList.toggle('on', s === span);
      }
      refreshAll();
    });
  }
}

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

async function refreshAll() {
  const range = getPeriod();
  try {
    await whenChartReady();
    configureChartDefaults();
    const [summary, daily, byProject, byModel, tools] = await Promise.all([
      fetchSummary(range),
      fetchDailyTokens(range),
      fetchByProject(range),
      fetchByModel(range),
      fetchTools(range),
    ]);
    renderScorecard(summary);
    renderDailyWork(daily);
    renderDailyCache(daily);
    renderByProject(byProject);
    renderByModel(byModel);
    renderToolMix(tools);
  } catch (err) {
    console.error('[breakdown] refresh failed', err);
  }
}

// ---------------------------------------------------------------------------
// SSE refresh
//
// The existing /api/stream stream is parameterised by 'period' (today/7d/30d/all),
// not our 'range', and its payload is tailored to Overview. We ignore the payload
// and only use the message event as a tick signal, then re-fetch our own endpoints
// with the currently-selected range.
// ---------------------------------------------------------------------------

let _es = null;
let _reconnectMs = 1000;

function connectSSE() {
  if (_es) { _es.close(); _es = null; }
  _es = new EventSource('/api/stream?period=today');
  _es.onopen = () => {
    _reconnectMs = 1000;
    const dot = document.getElementById('sse-dot');
    if (dot) { dot.className = 'sse-dot connected'; dot.title = 'Live — connected'; }
  };
  _es.onmessage = () => { refreshAll().catch(err => console.error('[breakdown] sse refresh', err)); };
  _es.onerror = () => {
    if (_es) { _es.close(); _es = null; }
    const dot = document.getElementById('sse-dot');
    if (dot) { dot.className = 'sse-dot amber'; dot.title = 'Live — reconnecting'; }
    setTimeout(connectSSE, _reconnectMs);
    _reconnectMs = Math.min(_reconnectMs * 2, 30_000);
  };
}

connectSSE();

wirePeriodPills();
_wireUnitPills('bd-time-unit-pills', _LS_BD_TIME_UNIT,
  () => _bdTimeUnit,
  v  => { _bdTimeUnit = v; },
  () => renderDailyWork(null),
);
_wireUnitPills('bd-project-unit-pills', _LS_BD_PROJECT_UNIT,
  () => _bdProjectUnit,
  v  => { _bdProjectUnit = v; },
  () => renderByProject(null),
);
_wireUnitPills('bd-model-unit-pills', _LS_BD_MODEL_UNIT,
  () => _bdModelUnit,
  v  => { _bdModelUnit = v; },
  () => renderByModel(null),
);
_wireUnitPills('bd-tools-unit-pills', _LS_BD_TOOL_UNIT,
  () => _bdToolUnit,
  v  => { _bdToolUnit = v; },
  () => renderToolMix(null),
);
loadThresholdsFromPrefs().then(() => refreshAll());
