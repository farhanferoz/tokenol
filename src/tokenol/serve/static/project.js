import {
  fmtUSD, fmtTok, fmtPct, fmtRatio, fmtAbsTime, shortModel, esc,
  verdictPill, renderVerdictDist, GLOSSARY,
} from '/assets/components.js';

const $ = id => document.getElementById(id);

const CV = {};
(function () {
  const s = getComputedStyle(document.documentElement);
  ['--amber', '--mute', '--rule', '--amber-dim'].forEach(n => { CV[n] = s.getPropertyValue(n).trim(); });
})();

const RANGE_LABELS = { '1d': '1d', '7d': '7d', '14d': '14d', '30d': '30d', 'all': 'all' };
const STORAGE_KEY = 'tokenol.project.range';
const cwd_b64 = location.pathname.split('/').pop();

let _range = localStorage.getItem(STORAGE_KEY) || '14d';
let _chartInst = null;

function _initGlossary() {
  const byTerm = Object.fromEntries(GLOSSARY.map(e => [e.term, e.def]));
  document.querySelectorAll('[data-term]').forEach(el => {
    const def = byTerm[el.dataset.term];
    if (def) el.title = def;
  });
}

function _fmtDuration(startISO, endISO) {
  if (!startISO || !endISO) return '–';
  const ms = new Date(endISO) - new Date(startISO);
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60), m = mins % 60;
  if (h < 24) return m ? `${h}h ${m}m` : `${h}h`;
  const d = Math.floor(h / 24), rh = h % 24;
  return rh ? `${d}d ${rh}h` : `${d}d`;
}

function loadData() {
  fetch(`/api/project/${cwd_b64}?range=${_range}`)
    .then(r => {
      if (r.status === 404) return null;
      if (!r.ok) throw new Error(r.status);
      return r.json();
    })
    .then(d => {
      if (!d) {
        renderEmpty();
      } else {
        renderAll(d);
      }
    })
    .catch(err => {
      const el = $('proj-error');
      el.style.display = '';
      el.textContent = `Failed to load project: ${esc(String(err.message))}`;
    });
}

function renderEmpty() {
  $('proj-grid').style.display = 'none';
  const sec = document.querySelector('.proj-section');
  if (sec) sec.style.display = 'none';
  const el = $('proj-error');
  el.style.display = '';
  el.style.color = 'var(--mute)';
  el.textContent = 'No activity in this range.';
  $('proj-cost').textContent = '$0.00';
  $('proj-sessions').textContent = '0';
}

function renderAll(d) {
  $('proj-grid').style.display = '';
  const sec = document.querySelector('.proj-section');
  if (sec) sec.style.display = '';
  $('proj-error').style.display = 'none';

  const name = d.cwd?.split('/').pop() || d.cwd || 'project';
  document.title = `tokenol — ${name}`;
  $('proj-name').textContent = name;
  $('proj-cwd').textContent  = d.cwd || '–';
  $('proj-cost').textContent = fmtUSD(d.total_cost);
  $('proj-sessions').textContent = d.session_count;

  if (d.flagged) $('proj-flagged-badge').style.display = '';

  const trendTitle = $('cache-trend-title');
  if (trendTitle) {
    const suffix = d.cache_trend_unit === 'hour' ? ' (hourly)' : '';
    trendTitle.textContent = `Cache efficiency — ${RANGE_LABELS[_range] || _range}${suffix}`;
  }

  renderCacheTrend(d.cache_trend, d.cache_trend_unit);
  renderHistogram(d.context_growth_histogram);
  renderVerdictDist('verdict-dist', d.verdict_distribution);
  renderTopTurns(d.top_turns_by_cost);
  renderProjectSessions(d.sessions);
  _initGlossary();
}

function renderCacheTrend(trend, unit) {
  if (!trend?.length) return;
  const cont = $('cache-trend-chart');
  if (!cont) return;

  if (_chartInst) { _chartInst.destroy(); _chartInst = null; }
  cont.innerHTML = '';

  // For 1d range the backend emits hourly buckets (ISO datetimes) instead of
  // date strings. Each row.date is either "YYYY-MM-DD" (day) or a full ISO
  // datetime (hour); parse accordingly so the x-axis renders meaningful ticks.
  const xs = trend.map(r => {
    const s = r.date;
    const iso = unit === 'hour' ? s : `${s}T00:00:00Z`;
    return new Date(iso).getTime() / 1000;
  });
  const ys = trend.map(r => r.hit_rate ?? 0);
  const AMBER_RGB = '255,182,71';

  _chartInst = new uPlot({
    width:  cont.offsetWidth || 500,
    height: 120,
    cursor: { show: false },
    legend: { show: false },
    padding: [4, 4, 0, 0],
    select: { show: false },
    axes: [
      { stroke: CV['--mute'], ticks: { show: false }, grid: { show: false }, size: 28 },
      { stroke: CV['--mute'], ticks: { show: false }, grid: { stroke: CV['--rule'], width: 1 }, size: 44,
        values: (_, vs) => vs.map(v => v != null ? `${Math.round(v*100)}%` : '') },
    ],
    series: [{}, { stroke: CV['--amber'], width: 1.5, fill: `rgba(${AMBER_RGB},0.14)`, label: 'hit%' }],
    scales: { y: { range: [0, 1] } },
  }, [xs, ys], cont);
}

function renderHistogram(histogram) {
  if (!histogram?.length) return;
  const bars = $('histogram-bars');
  const labels = $('histogram-labels');
  const maxCount = Math.max(...histogram.map(b => b.count), 1);
  bars.innerHTML = histogram.map(b => {
    const h = Math.max((b.count / maxCount) * 100, b.count > 0 ? 8 : 2);
    const cls = b.label.includes('2k') || b.label.includes('5k') || b.label.includes('10k') ? 'background:var(--alarm)' : '';
    return `<div class="hist-bar" style="height:${h}%;${cls}" title="${b.label}: ${b.count} sessions"></div>`;
  }).join('');
  if (labels) labels.innerHTML = histogram.map(b => `<span>${b.label}</span>`).join('');
}

function renderTopTurns(turns) {
  const tbody = $('top-turns-tbody');
  if (!tbody) return;
  tbody.innerHTML = (turns || []).slice(0, 15).map(t => {
    const hitPct  = t.hit_rate   != null ? fmtPct(t.hit_rate)   : '–';
    const cpkw    = t.cost_per_kw != null ? `$${(+t.cost_per_kw).toFixed(2)}` : '–';
    const ctx     = t.ctx_ratio  != null ? fmtRatio(t.ctx_ratio) : '–';
    return `<tr style="cursor:pointer" data-sess="${t.session_id}">
      <td class="mute">${fmtAbsTime(t.ts)}</td>
      <td class="mute">${t.session_id.slice(0,8)}</td>
      <td>${fmtUSD(t.cost_usd)}</td>
      <td>${hitPct}</td>
      <td>${cpkw}</td>
      <td>${ctx}</td>
      <td>${fmtTok(t.output_tokens)}</td>
    </tr>`;
  }).join('');
  tbody.querySelectorAll('tr[data-sess]').forEach(row => {
    row.addEventListener('click', () => { location.href = `/session/${row.dataset.sess}`; });
  });
}

function renderProjectSessions(sessions) {
  const tbody = $('proj-sessions-tbody');
  if (!tbody) return;
  tbody.innerHTML = (sessions || []).map(s => {
    const hitPct  = s.cache_hit_rate != null ? fmtPct(s.cache_hit_rate) : '–';
    const cpkw    = s.cost_per_kw   != null ? `$${(+s.cost_per_kw).toFixed(2)}` : '–';
    const ctx     = s.ctx_ratio     != null ? fmtRatio(s.ctx_ratio)     : '–';
    const toolErr = s.tool_error_rate > 0   ? fmtPct(s.tool_error_rate)  : '–';
    const dur     = _fmtDuration(s.first_ts, s.last_ts);
    const rowCls  = s.verdict !== 'OK' ? 'row-flagged-red' : '';
    return `<tr data-id="${s.id}" class="${rowCls}" style="cursor:pointer">
      <td>${fmtAbsTime(s.first_ts)}</td>
      <td class="mute">${dur}</td>
      <td>${shortModel(s.model)}</td>
      <td>${fmtUSD(s.cost_usd)}</td>
      <td>${s.turns}</td>
      <td>${hitPct}</td>
      <td>${cpkw}</td>
      <td>${ctx}</td>
      <td>${toolErr}</td>
      <td>${verdictPill(s.verdict)}</td>
      <td class="mute">${s.id.slice(0,8)}</td>
    </tr>`;
  }).join('');
  tbody.querySelectorAll('tr[data-id]').forEach(row => {
    row.addEventListener('click', () => { location.href = `/session/${row.dataset.id}`; });
  });
}

// Wire range pills
document.querySelectorAll('#proj-range-pills [data-range]').forEach(el => {
  el.classList.toggle('on', el.dataset.range === _range);
  el.addEventListener('click', () => {
    _range = el.dataset.range;
    localStorage.setItem(STORAGE_KEY, _range);
    document.querySelectorAll('#proj-range-pills [data-range]').forEach(b => b.classList.toggle('on', b === el));
    loadData();
  });
});

loadData();
