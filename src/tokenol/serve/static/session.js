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

// True when the session spans multiple UTC calendar days — triggers date-aware
// tick labels on charts and the turns table so '23:00 → 00:00' stops ambiguating.
let _multiDay = false;

function _dateShort(d) {
  return d.toLocaleDateString('en', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

function fmtTurnTs(isoStr) {
  const d = new Date(isoStr);
  const t = [d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds()]
    .map(n => String(n).padStart(2, '0')).join(':');
  return _multiDay ? `${_dateShort(d)} ${t}` : t;
}

function _xFmtTurn(v) {
  const d = new Date(v * 1000);
  const hm = `${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}`;
  if (!_multiDay) return d.getUTCMinutes() === 0 ? `${d.getUTCHours()}:00` : '';
  // Multi-day: show date on the first tick of each day (midnight ± tick granularity)
  if (d.getUTCHours() === 0 && d.getUTCMinutes() < 30) return _dateShort(d);
  return hm;
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

  // Set _multiDay first — every downstream tick/table formatter reads it.
  _totalTurns = (d.turns || []).length;
  _multiDay = d.first_ts && d.last_ts
    && new Date(d.first_ts).toUTCString().slice(0, 16) !== new Date(d.last_ts).toUTCString().slice(0, 16);

  $('sess-id').textContent = d.session_id;
  const vEl = $('sess-verdict');
  vEl.textContent = d.verdict;
  vEl.className   = `verdict-pill verdict-${d.verdict}`;

  if (d.first_ts && d.last_ts) {
    $('sess-time-range').textContent = `${fmtTurnTs(d.first_ts)} – ${fmtTurnTs(d.last_ts)} UTC`;
  }

  $('sess-cwd').textContent       = d.cwd || '–';
  $('sess-cwd').title             = d.cwd || '';
  $('sess-model').textContent     = shortModel(d.model);
  $('sess-cost').textContent      = fmtUSD(d.totals.cost_usd);
  $('sess-turns').textContent     = d.totals.turns;
  $('sess-tool-uses').textContent = d.totals.tool_uses;
  $('sess-tool-errors').textContent = d.totals.tool_errors;
  if (d.totals.tool_errors > 0) $('sess-tool-errors').classList.add('alarm');

  renderPatternCards(d.patterns || []);
  renderCostBars(d.turns);
  renderChart(d.turns);
  renderOutputChart(d.turns);
  renderContextChart(d.turns);
  renderCacheHitChart(d.turns);
  renderStopReasonStrip(d.turns);
  renderTimeline(d.turns, d.first_ts, d.last_ts);
  initTable(d.turns);
}

// ---- pattern cards ----

function renderPatternCards(patterns) {
  const section = $('pattern-section');
  const cont    = $('pattern-cards');
  if (!section || !cont) return;

  section.style.display = '';

  if (!patterns.length) {
    cont.innerHTML = '<div class="pattern-empty">No known problem patterns detected — this session looks healthy.</div>';
    return;
  }

  const GLYPH = { red: '⚠', amber: '⚠', info: 'ⓘ' };
  cont.innerHTML = patterns.map(p => {
    const glyph  = GLYPH[p.severity] || 'ⓘ';
    const first  = (p.turn_indices || [])[0];
    const n      = (p.turn_indices || []).length;
    const jumpHtml = (first != null)
      ? `<span class="jump-link" data-idx="${first}">Jump to first ↓</span>`
      : '';
    return `<div class="pattern-card pattern-sev-${p.severity}"
                data-turn-indices="${(p.turn_indices || []).join(',')}">
      <div class="pattern-glyph">${glyph}</div>
      <div class="pattern-body">
        <div class="pattern-headline">${esc(p.headline)}</div>
        <div class="pattern-reason">${esc(p.reason)}</div>
        <div class="pattern-fix">${esc(p.suggested_fix)}</div>
        <div class="pattern-jump">Triggered by ${n} turn${n === 1 ? '' : 's'}. ${jumpHtml}</div>
      </div>
    </div>`;
  }).join('');

  cont.querySelectorAll('.jump-link[data-idx]').forEach(link => {
    link.addEventListener('click', () => {
      const idx = +link.dataset.idx;
      const barsSection = $('cost-bars-section');
      if (barsSection) barsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
      _highlightCostBar(idx);
      openTurnModal(idx);
    });
  });
}

function _highlightCostBar(turnIdx) {
  const cont = $('cost-bars-chart');
  if (!cont) return;
  cont.querySelectorAll(`[data-idx="${turnIdx}"]`).forEach(rect => {
    rect.classList.add('bar-highlight');
    rect.addEventListener('animationend', () => rect.classList.remove('bar-highlight'), { once: true });
  });
}

// ---- turn drill-down modal ----

let _currentTurnIdx = null;
let _totalTurns = 0;

function openTurnModal(turnIdx) {
  const bg = $('turn-modal-bg');
  if (!bg) return;
  $('turn-modal-content').innerHTML = '<div style="color:var(--mute);padding:20px 0;">Loading…</div>';
  bg.style.display = 'block';
  _currentTurnIdx = turnIdx;
  _updateNavButtons();

  fetch(`/api/session/${sessionId}/turn/${turnIdx}`)
    .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(d => _renderTurnModal(d))
    .catch(err => {
      $('turn-modal-content').innerHTML = `<div style="color:var(--alarm)">Error: ${esc(String(err))}</div>`;
    });
}

function _closeTurnModal() {
  const bg = $('turn-modal-bg');
  if (bg) bg.style.display = 'none';
  _currentTurnIdx = null;
}

function _updateNavButtons() {
  const prev = $('turn-modal-prev');
  const next = $('turn-modal-next');
  if (prev) prev.style.opacity = _currentTurnIdx > 0 ? '1' : '0.3';
  if (next) next.style.opacity = (_currentTurnIdx != null && _currentTurnIdx < _totalTurns - 1) ? '1' : '0.3';
}

function _renderTurnModal(d) {
  const cc = d.cost_components || {};
  const tc = d.token_counts   || {};
  const tools = (d.tool_calls || []);

  const fmtCents = v => v >= 0.01 ? `$${v.toFixed(2)}` : v > 0 ? `$${v.toFixed(5)}` : '$0';
  const ts = new Date(d.ts);
  const tsStr = ts.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

  const miniBar = ['cache_read','input','cache_creation','output'].map(k => {
    const total = (cc.cache_read||0)+(cc.input||0)+(cc.cache_creation||0)+(cc.output||0);
    if (!total || !(cc[k]||0)) return '';
    const pct = (cc[k] / total * 100).toFixed(1);
    const col = k === 'cache_creation' ? 'var(--alarm)' : k === 'input' ? 'var(--amber-dim)' : k === 'output' ? 'var(--cool)' : 'var(--mute)';
    return `<div style="width:${pct}%;background:${col};height:100%;display:inline-block;"></div>`;
  }).join('');

  const toolsHtml = tools.length
    ? tools.map(t => `<span style="margin-right:8px;color:${t.ok ? 'var(--mute)' : 'var(--alarm)'}">${esc(t.name)} ${t.ok ? '✓' : '✗'}</span>`).join('')
    : '<span style="color:var(--mute)">none</span>';

  const promptHtml = d.user_prompt
    ? `<div style="background:var(--bg);padding:10px 12px;font-size:12px;white-space:pre-wrap;overflow-wrap:break-word;max-height:150px;overflow-y:auto;color:var(--mute)">${esc(d.user_prompt)}</div>`
    : '<div style="color:var(--mute);font-size:12px;">—</div>';

  const asstHtml = d.assistant_preview
    ? `<div style="background:var(--bg);padding:10px 12px;font-size:12px;white-space:pre-wrap;overflow-wrap:break-word;max-height:150px;overflow-y:auto;color:var(--mute)">${esc(d.assistant_preview)}</div>`
    : '<div style="color:var(--mute);font-size:12px;">—</div>';

  const sideLabel = d.is_sidechain ? ' <span style="font-size:10px;color:var(--mute);text-transform:uppercase;letter-spacing:0.06em;">sidechain</span>' : '';

  $('turn-modal-content').innerHTML = `
    <div style="margin-bottom:16px;">
      <span class="serif" style="font-style:italic;font-size:18px;">Turn ${d.turn_idx + 1}</span>
      <span style="font-size:12px;color:var(--mute);margin-left:12px;">${tsStr}</span>
      <span style="font-size:12px;color:var(--mute);margin-left:10px;">${esc(shortModel(d.model))}</span>
      <span style="font-size:12px;color:var(--mute);margin-left:10px;">${esc(d.stop_reason || '–')}${sideLabel}</span>
    </div>
    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:var(--mute);margin-bottom:4px;">Cost <span class="amber" style="font-size:16px;font-weight:600;">${fmtCents((cc.cache_read||0)+(cc.input||0)+(cc.cache_creation||0)+(cc.output||0))}</span></div>
      <div style="width:100%;height:6px;background:var(--rule);border-radius:3px;overflow:hidden;">${miniBar}</div>
      <div style="display:flex;gap:16px;margin-top:6px;font-size:11px;color:var(--mute);">
        ${['cache_read','input','cache_creation','output'].map(k => cc[k] > 0 ? `<span>${k.replace('_',' ')} <b>${fmtCents(cc[k])}</b></span>` : '').filter(Boolean).join('')}
      </div>
    </div>
    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:var(--mute);margin-bottom:4px;">Tokens</div>
      <div style="font-size:12px;display:flex;gap:16px;flex-wrap:wrap;">
        <span>in <b>${fmtTok(tc.input)}</b></span>
        <span>out <b>${fmtTok(tc.output)}</b></span>
        <span>read <b>${fmtTok(tc.cache_read)}</b></span>
        <span>creation <b>${fmtTok(tc.cache_creation)}</b></span>
      </div>
    </div>
    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:var(--mute);margin-bottom:4px;">Tools</div>
      <div style="font-size:12px;">${toolsHtml}</div>
    </div>
    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:var(--mute);margin-bottom:4px;">User prompt</div>
      ${promptHtml}
    </div>
    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:var(--mute);margin-bottom:4px;">Assistant preview</div>
      ${asstHtml}
    </div>
    <div style="font-size:10px;color:var(--mute);">Source: ${esc(d.source_file)}${d.source_line ? ':' + d.source_line : ''}</div>
  `;
}

// Close on X / backdrop click
$('turn-modal-close').addEventListener('click', _closeTurnModal);
$('turn-modal-bg').addEventListener('click', ev => {
  if (ev.target === $('turn-modal-bg')) _closeTurnModal();
});

// Prev / next navigation
$('turn-modal-prev').addEventListener('click', () => {
  if (_currentTurnIdx > 0) openTurnModal(_currentTurnIdx - 1);
});
$('turn-modal-next').addEventListener('click', () => {
  if (_currentTurnIdx != null && _currentTurnIdx < _totalTurns - 1) openTurnModal(_currentTurnIdx + 1);
});

// Keyboard: Esc close, ← / → navigate
document.addEventListener('keydown', ev => {
  const bg = $('turn-modal-bg');
  if (!bg || bg.style.display === 'none') return;
  if (ev.key === 'Escape') { _closeTurnModal(); return; }
  if (ev.key === 'ArrowLeft'  && _currentTurnIdx > 0) { ev.preventDefault(); openTurnModal(_currentTurnIdx - 1); }
  if (ev.key === 'ArrowRight' && _currentTurnIdx != null && _currentTurnIdx < _totalTurns - 1) { ev.preventDefault(); openTurnModal(_currentTurnIdx + 1); }
});

// ---- cost per turn: small multiples ----

// Four strip charts stacked vertically. Each strip has its own Y-scale so a
// rare $2 cache_creation spike doesn't flatten a turn's $0.03 input component.
// A shared vertical cursor ties the rows together; hovering any bar highlights
// the turn across all four strips.
const _CBAR_STRIPS = [
  { key: 'cache_creation', label: 'cache creation', color: 'var(--alarm)'     },
  { key: 'cache_read',     label: 'cache read',     color: 'var(--amber-dim)' },
  { key: 'input',          label: 'input',          color: 'var(--amber-dim)' },
  { key: 'output',         label: 'output',         color: 'var(--cool)'      },
];

function _fmtCostCompact(v) {
  if (v >= 1)    return `$${v.toFixed(v >= 10 ? 0 : 1)}`;
  if (v >= 0.1)  return `$${v.toFixed(2)}`;
  if (v >= 0.01) return `$${v.toFixed(3)}`;
  return `$${v.toFixed(4)}`;
}

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
  const visible = top30
    ? turns.map((t, i) => ({t, i}))
        .sort((a, b) => b.t.cost_usd - a.t.cost_usd).slice(0, 30)
        .sort((a, b) => a.i - b.i)
    : turns.map((t, i) => ({t, i}));

  const n = visible.length;
  if (!n) { cont.innerHTML = ''; return; }

  // Skip strips whose max is 0 (e.g. all input was cached, so cost_components.input = 0).
  // Strips with zero data are visual noise and confuse the reader with "max $0.0000".
  const activeStrips = _CBAR_STRIPS
    .map(s => ({...s, max: Math.max(...visible.map(e => e.t.cost_components?.[s.key] || 0), 0)}))
    .filter(s => s.max > 0);
  if (!activeStrips.length) { cont.innerHTML = ''; return; }

  const LABEL_W = 130;
  const STRIP_H = 64;
  const GAP_Y   = 8;
  const AXIS_H  = 20;
  const stripTop = si => si * (STRIP_H + GAP_Y);
  const plotAreaH = activeStrips.length * (STRIP_H + GAP_Y);
  const TOTAL_H = plotAreaH + AXIS_H;
  const W = cont.offsetWidth || 800;
  const plotW = Math.max(200, W - LABEL_W - 8);
  const barW = Math.max(1, Math.min(14, Math.floor(plotW / n) - 1));
  const gap  = Math.max(0, Math.floor(barW * 0.15));
  const stride = barW + gap;
  const plotUsedW = n * stride;

  const parts = [];

  activeStrips.forEach((strip, si) => {
    const maxV = strip.max;
    const scale = v => Math.max(1, (v / maxV) * (STRIP_H - 4));
    const yTop = stripTop(si);
    const baseY = yTop + STRIP_H;

    // Row background + baseline
    parts.push(
      `<rect x="${LABEL_W}" y="${yTop}" width="${plotUsedW}" height="${STRIP_H}" fill="var(--bg-raised)"/>`,
      `<line x1="${LABEL_W}" x2="${LABEL_W + plotUsedW}" y1="${baseY}" y2="${baseY}" stroke="var(--rule)"/>`,
      `<text x="${LABEL_W - 8}" y="${yTop + 13}" text-anchor="end" font-size="11" fill="var(--fg)">${strip.label}</text>`,
      `<text x="${LABEL_W - 8}" y="${yTop + 27}" text-anchor="end" font-size="10" fill="var(--mute)">max ${_fmtCostCompact(maxV)}</text>`,
    );

    visible.forEach((e, j) => {
      const v = e.t.cost_components?.[strip.key] || 0;
      if (v <= 0) return;
      const h = scale(v);
      const x = LABEL_W + j * stride;
      const y = baseY - h;
      parts.push(
        `<rect data-idx="${e.i}" x="${x}" y="${y.toFixed(1)}" width="${barW}" `
        + `height="${h.toFixed(1)}" fill="${strip.color}" style="cursor:pointer"></rect>`
      );
    });
  });

  // X-axis time labels — 5 ticks across the span. In multi-day sessions,
  // prepend the date on the first tick and whenever the UTC date changes
  // between ticks; otherwise the reader sees "23:00, 00:00, 01:00" with no way
  // to tell they're straddling midnight.
  const tickCount = 5;
  let prevDateStr = null;
  for (let ti = 0; ti <= tickCount; ti++) {
    const j = Math.min(n - 1, Math.floor(ti * (n - 1) / tickCount));
    const turn = visible[j]?.t;
    if (!turn) continue;
    const x = LABEL_W + j * stride + barW / 2;
    const d = new Date(turn.ts);
    const dateStr = d.toISOString().slice(0, 10);
    const hm = [d.getUTCHours(), d.getUTCMinutes()].map(k => String(k).padStart(2,'0')).join(':');
    const showDate = _multiDay && (ti === 0 || dateStr !== prevDateStr);
    const lbl = showDate ? `${_dateShort(d)} ${hm}` : hm;
    prevDateStr = dateStr;
    parts.push(
      `<line x1="${x}" x2="${x}" y1="${plotAreaH}" y2="${plotAreaH + 4}" stroke="var(--mute)"/>`,
      `<text x="${x}" y="${plotAreaH + 14}" text-anchor="middle" font-size="10" fill="var(--mute)">${lbl}</text>`,
    );
  }

  // Shared vertical cursor (hidden until hover)
  parts.push(
    `<line id="cbar-cursor" x1="0" y1="0" x2="0" y2="${plotAreaH}" stroke="var(--fg)" stroke-dasharray="2 2" opacity="0" pointer-events="none"/>`
  );

  const svgW = Math.max(W, LABEL_W + plotUsedW);
  cont.innerHTML = `<svg width="${svgW}" height="${TOTAL_H}" style="overflow:visible;display:block">${parts.join('')}</svg>`;

  const tip = document.createElement('div');
  tip.className = 'u-tooltip';
  tip.style.cssText = 'position:absolute;display:none;pointer-events:none;';
  cont.appendChild(tip);

  const svg    = cont.querySelector('svg');
  const cursor = svg.querySelector('#cbar-cursor');

  // Nearest-turn lookup from x-coordinate. Works even when hovering empty space.
  const nearestIdx = svgX => {
    const rel = svgX - LABEL_W;
    if (rel < 0) return -1;
    const j = Math.floor(rel / stride);
    return j >= 0 && j < n ? j : -1;
  };

  svg.addEventListener('mousemove', ev => {
    const pt  = svg.createSVGPoint();
    pt.x = ev.clientX; pt.y = ev.clientY;
    const m = svg.getScreenCTM();
    if (!m) return;
    const loc = pt.matrixTransform(m.inverse());
    const j = nearestIdx(loc.x);
    if (j < 0) { tip.style.display = 'none'; cursor.setAttribute('opacity', '0'); return; }

    const cursorX = LABEL_W + j * stride + barW / 2;
    cursor.setAttribute('x1', cursorX);
    cursor.setAttribute('x2', cursorX);
    cursor.setAttribute('opacity', '0.5');

    const origIdx = visible[j].i;
    const t  = turns[origIdx];
    const cc = t.cost_components || {};
    const d  = new Date(t.ts);
    const hm = [d.getUTCHours(), d.getUTCMinutes()].map(k => String(k).padStart(2,'0')).join(':');
    const lines = _CBAR_STRIPS
      .filter(s => (cc[s.key] || 0) > 0)
      .map(s => `<span class="tt-lbl">${s.label}</span> <span class="tt-val">${_fmtCostCompact(cc[s.key])}</span>`);
    tip.innerHTML = `<div class="tt-time">Turn ${origIdx + 1} · ${hm} UTC</div>${lines.join('<br>')}` +
      `<br><span class="tt-lbl">total</span> <span class="tt-val">${_fmtCostCompact(t.cost_usd)}</span>`;
    tip.style.display = '';
    const bRect = cont.getBoundingClientRect();
    const tipW  = tip.offsetWidth || 140;
    const left  = Math.min(ev.clientX - bRect.left + 12, (cont.offsetWidth || W) - tipW - 4);
    tip.style.left = `${left}px`;
    tip.style.top  = `${ev.clientY - bRect.top - (tip.offsetHeight || 60) - 6}px`;
  });
  svg.addEventListener('mouseleave', () => {
    tip.style.display = 'none';
    cursor.setAttribute('opacity', '0');
  });
  svg.addEventListener('click', ev => {
    const pt = svg.createSVGPoint();
    pt.x = ev.clientX; pt.y = ev.clientY;
    const m = svg.getScreenCTM();
    if (!m) return;
    const loc = pt.matrixTransform(m.inverse());
    const j = nearestIdx(loc.x);
    if (j >= 0) openTurnModal(visible[j].i);
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
    stepped: true, xFmt: _xFmtTurn,
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
    stepped: true, xFmt: _xFmtTurn,
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
      { stroke: CV['--mute'], ticks: { show: false }, grid: { show: false }, size: 28,
        values: (_, vs) => vs.map(_xFmtTurn) },
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
      { stroke: CV['--mute'], ticks: { show: false }, grid: { show: false }, size: 28,
        values: (_, vs) => vs.map(_xFmtTurn) },
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
      <td>${fmtTurnTs(t.ts)}</td>
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
