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
    const summary = await fetchSummary(range);
    renderScorecard(summary);
  } catch (err) {
    console.error('[breakdown] summary failed', err);
  }
}

wirePeriodPills();
refreshAll();
