// Breakdown page entry point. Loaded as an ES module by breakdown.html.
//
// Responsibilities (added across PR1):
//  - Period pill state (sessionStorage, independent of Overview)
//  - Scorecard fetch + render
//  - Chart.js global defaults (from CSS design tokens)
//  - Two Time-section charts
//  - SSE-driven refresh

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

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Tokenol dataset color cycle, semantic.
// 0 → amber (input, primary), 1 → alarm (output), 2 → green (cache),
// 3 → cool (model axis), 4 → mute, 5 → amber-dim.
function tokenolPalette() {
  return [
    cssVar('--amber'),
    cssVar('--alarm'),
    cssVar('--green'),
    cssVar('--cool'),
    cssVar('--mute'),
    cssVar('--amber-dim'),
  ];
}

// ---------------------------------------------------------------------------
// Cache-health thresholds (hard-coded from metrics/thresholds.DEFAULTS).
// PR2 ships these inline; a later task can fetch /api/prefs for live values.
// ---------------------------------------------------------------------------

const HIT_PCT_GREEN = 95.0;
const HIT_PCT_RED = 85.0;

function healthColorForHitRate(rate) {
  // `rate` is a decimal in [0, 1] or null/undefined.
  if (rate == null) return cssVar('--mute');
  const pct = rate * 100;
  if (pct >= HIT_PCT_GREEN) return cssVar('--green');
  if (pct >= HIT_PCT_RED) return cssVar('--amber');
  return cssVar('--alarm');
}

let _captionRendered = false;
function renderByProjectCaption() {
  if (_captionRendered) return;
  const el = document.getElementById('bp-by-project-caption');
  if (!el) return;
  el.innerHTML =
    `<span>cache hit rate</span>` +
    `<span class="caption-swatch caption-swatch--green"></span><span>≥${HIT_PCT_GREEN}%</span>` +
    `<span class="caption-swatch caption-swatch--amber"></span><span>${HIT_PCT_RED}–${HIT_PCT_GREEN}%</span>` +
    `<span class="caption-swatch caption-swatch--alarm"></span><span>&lt;${HIT_PCT_RED}%</span>`;
  _captionRendered = true;
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

function renderByProject(data) {
  renderByProjectCaption();
  const pal = tokenolPalette();
  // Cap to a readable number of bars; tail is dropped (not collapsed) so the
  // chart stays legible. Subheading notes how many were shown vs. total.
  const projects = data.projects.slice(0, BY_PROJECT_TOP_N);
  const labels = projects.map(p => p.project);
  const dotColors = projects.map(p => healthColorForHitRate(p.cache_hit_rate));
  const cwdB64 = projects.map(p => p.cwd_b64);
  const hitRate = projects.map(p => p.cache_hit_rate);

  const datasets = [
    { label: 'input',  data: projects.map(p => p.input),  backgroundColor: pal[0] },
    { label: 'output', data: projects.map(p => p.output), backgroundColor: pal[1] },
  ];

  const shownTotal = projects.reduce((s, p) => s + p.input + p.output, 0);
  const allTotal = data.projects.reduce((s, p) => s + p.input + p.output, 0);
  const subEl = document.getElementById('bp-by-project-sub');
  if (data.projects.length > BY_PROJECT_TOP_N) {
    const pct = Math.round((shownTotal / Math.max(allTotal, 1)) * 100);
    subEl.textContent = `top ${BY_PROJECT_TOP_N} of ${data.projects.length} · ${pct}% of billable`;
  } else {
    subEl.textContent = `${data.projects.length} project${data.projects.length === 1 ? '' : 's'}`;
  }

  const canvas = document.getElementById('chart-by-project');

  if (_chartByProject) {
    _chartByProject.$tokenol = { cwdB64, hitRate };
    _chartByProject.data.labels = labels;
    for (let i = 0; i < datasets.length; i++) {
      _chartByProject.data.datasets[i].data = datasets[i].data;
      _chartByProject.data.datasets[i].backgroundColor = datasets[i].backgroundColor;
    }
    _chartByProject.options.plugins.cacheHealthDots.colors = dotColors;
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
      layout: { padding: { bottom: 46 } },  // room for rotated labels + dot band
      scales: {
        x: {
          ticks: { maxRotation: 45, minRotation: 45, autoSkip: false, padding: 4 },
        },
        y: { beginAtZero: true, ticks: { callback: v => fmtTok(v) } },
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

function collapseModels(models) {
  // Keep top N−1, collapse the tail into 'others' only if it would exceed N.
  if (models.length <= BY_MODEL_TOP_N) {
    return models.map(m => ({ name: m.model, value: m.input + m.output, isOthers: false }));
  }
  const head = models.slice(0, BY_MODEL_TOP_N - 1).map(m => ({
    name: m.model, value: m.input + m.output, isOthers: false,
  }));
  const tailValue = models.slice(BY_MODEL_TOP_N - 1).reduce((s, m) => s + m.input + m.output, 0);
  head.push({ name: 'others', value: tailValue, isOthers: true });
  return head;
}

function renderByModel(data) {
  const pal = tokenolPalette();
  const collapsed = collapseModels(data.models);
  const labels = collapsed.map(c => c.name);
  const values = collapsed.map(c => c.value);
  const colors = collapsed.map((_, i) => pal[i % pal.length]);
  const names = collapsed.map(c => (c.isOthers ? null : c.name));

  document.getElementById('bp-by-model-sub').textContent =
    `${data.models.length} model${data.models.length === 1 ? '' : 's'}`;

  const canvas = document.getElementById('chart-by-model');

  if (_chartByModel) {
    _chartByModel.$tokenol = { names };
    _chartByModel.data.labels = labels;
    _chartByModel.data.datasets[0].data = values;
    _chartByModel.data.datasets[0].backgroundColor = colors;
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
            label(ctx) {
              const total = ctx.dataset.data.reduce((s, v) => s + v, 0) || 1;
              const v = ctx.parsed;
              return `${ctx.label}: ${fmtTok(v)} billable (${fmtPct((v / total) * 100)})`;
            },
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

let _chartTools = null;

function renderToolMix(data) {
  const pal = tokenolPalette();
  const tools = data.tools || [];
  const labels = tools.map(t => t.tool);
  const counts = tools.map(t => t.count);

  const subEl = document.getElementById('bp-tools-sub');
  const totalCalls = counts.reduce((s, n) => s + n, 0);
  subEl.textContent = tools.length === 0
    ? 'no tool calls'
    : `${tools.length} tool${tools.length === 1 ? '' : 's'} · ${fmtInt(totalCalls)} calls`;

  const canvas = document.getElementById('chart-tools');
  if (_chartTools) {
    _chartTools.data.labels = labels;
    _chartTools.data.datasets[0].data = counts;
    _chartTools.update('none');
    return;
  }
  _chartTools = new window.Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'calls',
        data: counts,
        backgroundColor: pal[0],
        borderWidth: 0,
      }],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { beginAtZero: true, ticks: { callback: v => fmtInt(v) } },
        y: { ticks: { autoSkip: false } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label(ctx) {
              return `${ctx.label}: ${fmtInt(ctx.parsed.x)} calls`;
            },
          },
        },
      },
    },
  });
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

function renderDailyWork(data) {
  const pal = tokenolPalette();
  const labels = data.days.map(d => d.date);
  const datasets = [
    { label: 'input',          data: data.days.map(d => d.input),          backgroundColor: pal[0] },
    { label: 'output',         data: data.days.map(d => d.output),         backgroundColor: pal[1] },
    { label: 'cache created',  data: data.days.map(d => d.cache_creation), backgroundColor: pal[2] },
  ];

  const totalCost = data.days.reduce((s, d) => s + d.cost_usd, 0);
  const days = Math.max(1, data.days.length);
  document.getElementById('bp-daily-work-sub').textContent =
    `total ${fmtUSD(totalCost)} · avg ${fmtUSD(totalCost / days)}/d`;

  const canvas = document.getElementById('chart-daily-work');
  if (_chartDailyWork) {
    // In-place update: avoids canvas destroy/recreate flicker on SSE tick.
    _chartDailyWork.data.labels = labels;
    for (let i = 0; i < datasets.length; i++) {
      _chartDailyWork.data.datasets[i].data = datasets[i].data;
      _chartDailyWork.data.datasets[i].backgroundColor = datasets[i].backgroundColor;
    }
    _chartDailyWork.update('none');
    return;
  }
  _chartDailyWork = new window.Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { stacked: true, ticks: { maxRotation: 45, minRotation: 45, autoSkip: true, maxTicksLimit: 14 } },
        y: { stacked: true, beginAtZero: true, ticks: { callback: v => fmtTok(v) } },
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
refreshAll();
