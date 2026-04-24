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
  if (_chartDailyWork) { _chartDailyWork.destroy(); _chartDailyWork = null; }
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
  if (_chartDailyCache) { _chartDailyCache.destroy(); _chartDailyCache = null; }
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
    const [summary, daily] = await Promise.all([
      fetchSummary(range),
      fetchDailyTokens(range),
    ]);
    renderScorecard(summary);
    renderDailyWork(daily);
    renderDailyCache(daily);
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
