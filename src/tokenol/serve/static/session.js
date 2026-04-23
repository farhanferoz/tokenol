// ---- helpers ----

import { drawChart } from '/assets/chart.js';

const $ = id => document.getElementById(id);

const CV = {};
(function () {
  const s = getComputedStyle(document.documentElement);
  ['--amber', '--cool', '--alarm', '--mute', '--rule', '--amber-dim'].forEach(n => {
    CV[n] = s.getPropertyValue(n).trim();
  });
})();

const AMBER_RGB = '255,182,71';
const COOL_RGB  = '111,174,216';

const fmtUSD = v => `$${(+v || 0).toFixed(2)}`;
const fmtTok = v => +v >= 1e6 ? `${(+v/1e6).toFixed(1)}M` : +v >= 1e3 ? `${(+v/1e3).toFixed(0)}k` : String(+v || 0);

function shortModel(m) {
  return (m || '–').replace(/^claude-/, '').replace(/-\d{8}$/, '').slice(0, 16);
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function hmsUTC(isoStr) {
  const d = new Date(isoStr);
  return [d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds()]
    .map(n => String(n).padStart(2, '0')).join(':');
}

// ---- load ----

const sessionId = location.pathname.split('/').pop();

fetch(`/api/session/${sessionId}`)
  .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
  .then(render)
  .catch(err => {
    const el = $('session-error');
    el.style.display = '';
    el.textContent = `Failed to load session ${esc(sessionId)}: ${esc(String(err.message))}`;
  });

// ---- top-level render ----

function render(d) {
  document.title = `tokenol — ${d.session_id.slice(0, 8)}`;

  $('sess-id').textContent = d.session_id;
  const vEl = $('sess-verdict');
  vEl.textContent = d.verdict;
  vEl.className   = `verdict-pill verdict-${d.verdict}`;

  if (d.first_ts && d.last_ts) {
    $('sess-time-range').textContent = `${hmsUTC(d.first_ts)} – ${hmsUTC(d.last_ts)} UTC`;
  }

  $('sess-cwd').textContent       = d.cwd || '–';
  $('sess-cwd').title             = d.cwd || '';
  $('sess-model').textContent     = shortModel(d.model);
  $('sess-cost').textContent      = fmtUSD(d.totals.cost_usd);
  $('sess-turns').textContent     = d.totals.turns;
  $('sess-tool-uses').textContent = d.totals.tool_uses;
  $('sess-tool-errors').textContent = d.totals.tool_errors;
  if (d.totals.tool_errors > 0) $('sess-tool-errors').classList.add('alarm');

  renderCostBars(d.turns);
  renderChart(d.turns);
  renderOutputChart(d.turns);
  renderContextChart(d.turns);
  renderCacheHitChart(d.turns);
  renderStopReasonStrip(d.turns);
  renderTimeline(d.turns, d.first_ts, d.last_ts);
  initTable(d.turns);
}

// ---- turn modal (wired in Task 8) ----

function openTurnModal(_turnIdx) { /* Task 8 */ }

// ---- cost per turn bars ----

const _CBAR_KEYS   = ['cache_read', 'input', 'cache_creation', 'output'];
const _CBAR_COLORS = () => ({
  cache_read:     CV['--mute'],
  input:          CV['--amber-dim'],
  cache_creation: CV['--alarm'],
  output:         CV['--cool'],
});

function renderCostBars(turns) {
  const section = $('cost-bars-section');
  const cont    = $('cost-bars-chart');
  if (!section || !cont || !turns.length) { if (section) section.style.display = 'none'; return; }

  let _top30 = false;
  const pills = section.querySelectorAll('[data-cbar-range]');
  pills.forEach(pill => {
    pill.addEventListener('click', () => {
      _top30 = pill.dataset.cbarRange === 'top30';
      pills.forEach(p => p.classList.toggle('on', p === pill));
      _drawCostBars(turns, cont, _top30);
    });
  });

  _drawCostBars(turns, cont, _top30);
}

function _drawCostBars(turns, cont, top30) {
  const colors = _CBAR_COLORS();
  let visible;
  if (top30) {
    visible = turns.map((t, i) => ({t, i}))
      .sort((a, b) => b.t.cost_usd - a.t.cost_usd).slice(0, 30)
      .sort((a, b) => a.i - b.i);
  } else {
    visible = turns.map((t, i) => ({t, i}));
  }

  const H = 160;
  const W = cont.offsetWidth || 800;
  const n = visible.length;
  if (!n) { cont.innerHTML = ''; return; }
  const barW = Math.max(2, Math.min(20, Math.floor((W - 4) / n) - 1));
  const gap  = Math.max(0, Math.floor(barW * 0.15));
  const maxCost = Math.max(...visible.map(e => e.t.cost_usd), 1e-9);

  let rects = '';
  visible.forEach((e, j) => {
    const t  = e.t;
    const cc = t.cost_components || {};
    const x  = j * (barW + gap);
    let y = H;
    _CBAR_KEYS.forEach(k => {
      const v = cc[k] || 0;
      if (v <= 0) return;
      const h = Math.max(1, (v / maxCost) * H);
      y -= h;
      rects += `<rect data-idx="${e.i}" x="${x}" y="${y.toFixed(1)}" `
        + `width="${barW}" height="${h.toFixed(1)}" fill="${colors[k]}" `
        + `data-k="${k}" data-v="${v.toFixed(5)}" data-total="${t.cost_usd.toFixed(5)}" `
        + `data-ts="${t.ts}" style="cursor:pointer"></rect>`;
    });
  });

  const svgW = Math.max(W, n * (barW + gap));
  cont.innerHTML = `<svg width="${svgW}" height="${H}" style="overflow:visible;display:block">${rects}</svg>`;

  let tip = document.createElement('div');
  tip.className = 'u-tooltip';
  tip.style.cssText = 'position:absolute;display:none;pointer-events:none;';
  cont.appendChild(tip);

  const svg = cont.querySelector('svg');
  svg.addEventListener('mousemove', ev => {
    const rect = ev.target.closest('[data-idx]');
    if (!rect) { tip.style.display = 'none'; return; }
    const idx = +rect.dataset.idx;
    const t   = turns[idx];
    const cc  = t.cost_components || {};
    const d   = new Date(t.ts);
    const hm  = [d.getUTCHours(), d.getUTCMinutes()].map(n => String(n).padStart(2,'0')).join(':');
    const lines = _CBAR_KEYS
      .filter(k => (cc[k] || 0) > 0)
      .map(k => `<span class="tt-lbl">${k.replace('_',' ')}</span> <span class="tt-val">$${cc[k].toFixed(4)}</span>`);
    tip.innerHTML = `<div class="tt-time">Turn ${idx+1} · ${hm} UTC</div>${lines.join('<br>')}` +
      `<br><span class="tt-lbl">total</span> <span class="tt-val">$${t.cost_usd.toFixed(4)}</span>`;
    tip.style.display = '';
    const bRect = cont.getBoundingClientRect();
    const tipW  = tip.offsetWidth || 130;
    const left  = Math.min(ev.clientX - bRect.left + 12, (cont.offsetWidth || W) - tipW - 4);
    tip.style.left = `${left}px`;
    tip.style.top  = `${ev.clientY - bRect.top - (tip.offsetHeight || 60) - 6}px`;
  });
  svg.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  svg.addEventListener('click', ev => {
    const rect = ev.target.closest('[data-idx]');
    if (rect) openTurnModal(+rect.dataset.idx);
  });
}

// ---- cache read per turn chart ----

function renderChart(turns) {
  if (!turns.length) { $('session-chart-section').style.display = 'none'; return; }
  const cont = $('turn-chart');
  const xs   = turns.map(t => new Date(t.ts).getTime() / 1000);
  const crd  = turns.map(t => t.cache_read_tokens);
  drawChart(cont, {
    xs, ySeries: [crd], labels: ['cache read'], yUnit: 'tokens', height: 180,
    stepped: true,
    onPointClick: idx => highlightTurn(idx),
  });
}

// ---- output tokens per turn chart ----

function renderOutputChart(turns) {
  if (!turns.length) { $('output-chart-section').style.display = 'none'; return; }
  const cont = $('output-chart');
  const xs  = turns.map(t => new Date(t.ts).getTime() / 1000);
  const out = turns.map(t => t.output_tokens);
  drawChart(cont, {
    xs, ySeries: [out], labels: ['output'], yUnit: 'tokens', height: 120,
    stepped: true,
    onPointClick: idx => highlightTurn(idx),
  });
}

// ---- context trajectory chart ----

function _lsSlope(xs, ys) {
  const n = xs.length;
  if (n < 2) return 0;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) { num += (xs[i]-mx)*(ys[i]-my); den += (xs[i]-mx)**2; }
  return den > 0 ? num/den : 0;
}

function renderContextChart(turns) {
  if (!turns.length) { $('context-chart-section').style.display = 'none'; return; }
  const cont = $('context-chart');

  const xs    = turns.map(t => new Date(t.ts).getTime() / 1000);
  const ctxPt = turns.map(t => t.input_tokens + t.cache_read_tokens + t.cache_creation_tokens);
  const slope = _lsSlope(Array.from({length:turns.length},(_,i)=>i), ctxPt);
  const slopeLine = ctxPt.map((_, i) => ctxPt[0] + slope * i);

  const badge = $('growth-rate-badge');
  if (badge) {
    const k = (slope / 1000).toFixed(1);
    const cls = slope > 5000 ? 'alarm' : slope > 2000 ? 'amber' : 'mute';
    badge.innerHTML = `<span class="${cls}">${slope > 0 ? '+':''} ${k}k tokens/turn</span>`;
  }

  new uPlot({
    width:  cont.offsetWidth || 800,
    height: 180,
    cursor: { show: true, points: { size: 6, fill: CV['--bg'] } },
    legend: { show: false },
    padding: [4, 4, 0, 0],
    select: { show: false },
    axes: [
      { stroke: CV['--mute'], ticks: { show: false }, grid: { show: false }, size: 28 },
      { stroke: CV['--mute'], ticks: { show: false }, grid: { stroke: CV['--rule'], width: 1 }, size: 60 },
    ],
    series: [
      {},
      { stroke: CV['--amber'], width: 1.5, fill: `rgba(${AMBER_RGB},0.12)`, label: 'ctx tokens' },
      { stroke: CV['--cool'],  width: 1,   dash: [4,4], label: 'slope' },
    ],
    scales: { y: { range: (_, _min, max) => [0, Math.max(max * 1.1, 100)] } },
  }, [xs, ctxPt, slopeLine], cont);
}

// ---- cache hit rate per turn ----

function _movingAvg(arr, window) {
  return arr.map((_, i) => {
    const start = Math.max(0, i - Math.floor(window/2));
    const end   = Math.min(arr.length, start + window);
    const slice = arr.slice(start, end);
    return slice.reduce((a,b)=>a+b,0)/slice.length;
  });
}

function renderCacheHitChart(turns) {
  if (!turns.length) { $('cache-hit-section').style.display = 'none'; return; }
  const cont = $('cache-hit-chart');

  const hitRates = turns.map(t => {
    const denom = t.input_tokens + t.cache_read_tokens + t.cache_creation_tokens;
    return denom > 0 ? t.cache_read_tokens / denom : 0;
  });
  const smoothed = _movingAvg(hitRates, 5);
  const xs = turns.map(t => new Date(t.ts).getTime() / 1000);

  new uPlot({
    width:  cont.offsetWidth || 800,
    height: 120,
    cursor: { show: true, points: { size: 6, fill: CV['--bg'] } },
    legend: { show: false },
    padding: [4, 4, 0, 0],
    select: { show: false },
    axes: [
      { stroke: CV['--mute'], ticks: { show: false }, grid: { show: false }, size: 28 },
      { stroke: CV['--mute'], ticks: { show: false }, grid: { stroke: CV['--rule'], width: 1 }, size: 44,
        values: (_, vs) => vs.map(v => v != null ? `${Math.round(v*100)}%` : '') },
    ],
    series: [{}, { stroke: CV['--cool'], width: 1.5, fill: `rgba(${COOL_RGB},0.14)`, label: 'hit%' }],
    scales: { y: { range: [0, 1] } },
  }, [xs, smoothed], cont);
}

// ---- stop reason strip ----

function renderStopReasonStrip(turns) {
  const section = $('stop-reason-section');
  const strip   = $('stop-reason-strip');
  const legend  = $('stop-reason-legend');
  if (!section || !strip || !legend) return;

  const reasons = turns.map(t => t.stop_reason || 'unknown');
  const counts  = {};
  for (const r of reasons) counts[r] = (counts[r] ?? 0) + 1;
  if (Object.keys(counts).length === 0) return;
  section.style.display = '';

  const COLORS = {
    'end_turn': 'var(--mute)',
    'tool_use': 'var(--amber)',
    'max_tokens': 'var(--alarm)',
    'refusal': '#e74c3c',
    'unknown': 'var(--rule)',
  };

  const total = reasons.length;
  const total_w = strip.offsetWidth || 600;

  // Build SVG bar
  let svgParts = '';
  let x = 0;
  for (const [r, cnt] of Object.entries(counts)) {
    const w = (cnt / total) * 100;
    const color = COLORS[r] || 'var(--cool)';
    svgParts += `<rect x="${x.toFixed(2)}%" y="0" width="${w.toFixed(2)}%" height="16"
      fill="${color}" title="${r}: ${cnt}"></rect>`;
    x += w;
  }
  strip.innerHTML = `<svg width="100%" height="16" style="display:block">${svgParts}</svg>`;

  legend.innerHTML = Object.entries(counts).map(([r, cnt]) => {
    const color = COLORS[r] || 'var(--cool)';
    return `<span style="display:flex;align-items:center;gap:4px;">
      <span style="width:12px;height:4px;background:${color};border-radius:2px;display:inline-block;"></span>
      <span style="color:var(--mute)">${r} (${cnt})</span>
    </span>`;
  }).join('');
}

// ---- tool-use timeline (SVG) ----

function renderTimeline(turns, firstTs, lastTs) {
  if (!turns.some(t => t.tool_use_count > 0)) {
    $('tool-timeline-section').style.display = 'none';
    return;
  }

  const svg  = $('tool-timeline-svg');
  const W    = svg.parentElement.clientWidth || 800;
  const H    = 48;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', W);

  const t0   = new Date(firstTs).getTime();
  const t1   = new Date(lastTs).getTime();
  const span = Math.max(t1 - t0, 1);
  const toX  = ms => Math.round(((ms - t0) / span) * (W - 20) + 10);

  const mk = tag => document.createElementNS('http://www.w3.org/2000/svg', tag);

  const axis = mk('line');
  axis.setAttribute('x1', 10);  axis.setAttribute('x2', W - 10);
  axis.setAttribute('y1', H/2); axis.setAttribute('y2', H/2);
  axis.setAttribute('stroke', 'var(--rule)');
  axis.setAttribute('stroke-width', 1);
  svg.appendChild(axis);

  const tip = document.createElement('div');
  tip.className = 'tl-tip';
  document.body.appendChild(tip);

  turns.forEach((t, i) => {
    if (!t.tool_use_count) return;

    const x      = toX(new Date(t.ts).getTime());
    const hasErr = t.tool_error_count > 0;
    const g      = mk('g');

    const hit = mk('rect');
    hit.setAttribute('x', x - 6); hit.setAttribute('y', 0);
    hit.setAttribute('width', 12); hit.setAttribute('height', H);
    hit.setAttribute('fill', 'transparent');

    const tick = mk('rect');
    tick.setAttribute('x', x - 1.5); tick.setAttribute('y', H/2 - 12);
    tick.setAttribute('width', 3);    tick.setAttribute('height', 24);
    tick.setAttribute('rx', 1.5);
    tick.setAttribute('fill', hasErr ? 'var(--alarm)' : 'var(--amber)');
    tick.setAttribute('opacity', hasErr ? '1' : '0.65');

    g.appendChild(hit);
    g.appendChild(tick);
    g.style.cursor = 'pointer';

    g.addEventListener('mouseenter', ev => {
      const errStr = hasErr
        ? ` · <span style="color:var(--alarm)">${t.tool_error_count} err</span>`
        : '';
      tip.innerHTML = `Turn ${i + 1} · ${t.tool_use_count}t${errStr}`;
      tip.style.display = 'block';
      tip.style.left = `${ev.clientX + 12}px`;
      tip.style.top  = `${ev.clientY - 8}px`;
    });
    g.addEventListener('mousemove', ev => {
      tip.style.left = `${ev.clientX + 12}px`;
      tip.style.top  = `${ev.clientY - 8}px`;
    });
    g.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
    g.addEventListener('click', () => highlightTurn(i));

    svg.appendChild(g);
  });
}

// ---- turn table ----

const PAGE = 100;
let _turns        = [];
let _origIndexMap = new Map();
let _sortKey      = 'ts';
let _sortAsc      = true;
let _page         = 0;

function initTable(turns) {
  _turns = turns;
  _origIndexMap = new Map(turns.map((t, i) => [t, i]));

  document.querySelectorAll('#turns-table th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (_sortKey === key) { _sortAsc = !_sortAsc; }
      else { _sortKey = key; _sortAsc = true; }
      _page = 0;
      renderTable();
    });
  });

  renderTable();
}

function sortedTurns() {
  return [..._turns].sort((a, b) => {
    let av, bv;
    if      (_sortKey === 'ts')     { av = a.ts;         bv = b.ts; }
    else if (_sortKey === 'cost')   { av = a.cost_usd;   bv = b.cost_usd; }
    else if (_sortKey === 'input')  { av = a.input_tokens + a.cache_creation_tokens;
                                      bv = b.input_tokens + b.cache_creation_tokens; }
    else if (_sortKey === 'tools')  { av = a.tool_use_count;   bv = b.tool_use_count; }
    else if (_sortKey === 'errors') { av = a.tool_error_count; bv = b.tool_error_count; }
    else { av = a.ts; bv = b.ts; }
    if (av < bv) return _sortAsc ? -1 :  1;
    if (av > bv) return _sortAsc ?  1 : -1;
    return 0;
  });
}

function renderTable() {
  const all   = sortedTurns();
  const total = all.length;
  const pages = Math.ceil(total / PAGE);
  const slice = all.slice(_page * PAGE, (_page + 1) * PAGE);

  $('table-info').textContent = total > PAGE
    ? `${_page * PAGE + 1}–${Math.min((_page + 1) * PAGE, total)} of ${total}`
    : `${total} turns`;

  // update sort indicators on headers
  document.querySelectorAll('#turns-table th[data-sort]').forEach(th => {
    th.classList.toggle('sort-asc',  th.dataset.sort === _sortKey &&  _sortAsc);
    th.classList.toggle('sort-desc', th.dataset.sort === _sortKey && !_sortAsc);
  });

  $('turns-tbody').innerHTML = slice.map((t, i) => {
    const origIdx = _origIndexMap.get(t);
    const rowNum  = _page * PAGE + i + 1;
    const errTd   = t.tool_error_count > 0
      ? `<td class="alarm">${t.tool_error_count}</td>`
      : `<td class="mute">–</td>`;
    return `<tr data-orig="${origIdx}">
      <td class="mute">${rowNum}</td>
      <td>${hmsUTC(t.ts)}</td>
      <td>${esc(shortModel(t.model))}</td>
      <td class="amber">${fmtUSD(t.cost_usd)}</td>
      <td>${fmtTok(t.input_tokens + t.cache_creation_tokens)}</td>
      <td class="mute">${fmtTok(t.cache_read_tokens)}</td>
      <td>${fmtTok(t.output_tokens)}</td>
      <td>${t.tool_use_count || '–'}</td>
      ${errTd}
      <td class="mute">${esc(t.stop_reason || '–')}</td>
    </tr>`;
  }).join('');

  // pagination (event delegation, no inline handlers)
  const pag = $('turn-pagination');
  if (pages <= 1) { pag.innerHTML = ''; return; }
  pag.innerHTML = Array.from({ length: pages }, (_, p) =>
    `<button class="${p === _page ? 'active' : ''}" data-p="${p}">${p + 1}</button>`
  ).join('');
}

$('turn-pagination').addEventListener('click', ev => {
  const btn = ev.target.closest('[data-p]');
  if (!btn) return;
  _page = parseInt(btn.dataset.p, 10);
  renderTable();
  $('session-table-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
});

// ---- cross-component highlight (chart click → table row) ----

function highlightTurn(origIdx) {
  const sorted    = sortedTurns();
  const sortedPos = sorted.findIndex(t => _origIndexMap.get(t) === origIdx);
  if (sortedPos < 0) return;

  const targetPage = Math.floor(sortedPos / PAGE);
  if (targetPage !== _page) { _page = targetPage; renderTable(); }

  document.querySelectorAll('#turns-tbody tr.highlighted').forEach(r => r.classList.remove('highlighted'));

  const row = document.querySelector(`#turns-tbody tr[data-orig="${origIdx}"]`);
  if (row) {
    row.classList.add('highlighted');
    row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}
