// ---- helpers ----

const $ = id => document.getElementById(id);

const CV = {};
function _initCssVars() {
  const s = getComputedStyle(document.documentElement);
  ['--amber','--cool','--alarm','--mute','--rule'].forEach(n => {
    CV[n] = s.getPropertyValue(n).trim();
  });
}
_initCssVars();

const fmtUSD  = v => `$${(+v || 0).toFixed(2)}`;
const fmtRate = v => `${fmtUSD(v)} / hr`;
const fmtPct  = v => `${(100 * (+v || 0)).toFixed(1)}%`;
const fmtDate = s => s ? s.slice(5, 10) : '–';
const fmtTok  = v => +v >= 1e6 ? `${(+v/1e6).toFixed(1)}M` : +v >= 1e3 ? `${(+v/1e3).toFixed(0)}k` : String(+v || 0);

function fmtDur(secs) {
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function shortModel(m) {
  return (m || '–').replace(/^claude-/, '').replace(/-\d{8}$/, '').slice(0, 14);
}

function modelColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360},50%,55%)`;
}

const AMBER_RGB = '255,182,71';

function hmColor(t) {
  if (t <= 0.001) return 'var(--rule)';
  return `rgba(${AMBER_RGB},${Math.min(0.12 + t * 0.88, 1).toFixed(2)})`;
}

// ---- state ----

let S = {};
const charts    = {};
const chartMeta = {};  // keyed by chart id → opts key; controls setData vs rebuild

let _idleTimer     = null;
let _todayTimestamp = 0;

const prefs = {
  burnLookback: '5m',
  sessionsRange: '24h',
  projectsRange: '24h',
  modelsRange:   '24h',
  dailyRange:    14,
  hideSidechain: false,
};

window.PREFS = prefs;
window.rerender = keys => render(new Set(keys));

// ---- merge + render ----

function mergeState(diff) {
  Object.assign(S, diff);
  resetIdleTimer();
  render(new Set(Object.keys(diff)));
}

function render(keys) {
  if (keys.has('active_window') || keys.has('config')) { renderGauge(); renderBurnHistory(); }
  if (keys.has('today'))         { renderToday(); renderHourlyBars(); }
  if (keys.has('daily_90d'))     { renderDailyArea(); }
  if (keys.has('sessions') || keys.has('_sess')) renderSessions();
  if (keys.has('models')   || keys.has('_mod'))  renderModels();
  if (keys.has('projects') || keys.has('_proj')) renderProjects();
  if (keys.has('heatmap_14d'))   renderHeatmap();
  if (keys.has('recent_turns'))  renderFeed();
}

// ---- gauge ----

function renderGauge() {
  const win = S.active_window;
  const ref  = S.config?.reference_usd ?? 50;
  const rate = win?.[`burn_rate_usd_per_hour_${prefs.burnLookback}`] ?? 0;
  const proj = win?.projected_window_cost ?? 0;

  $('main-gauge').setAll(
    rate.toFixed(4),
    proj.toFixed(4),
    ref,
    Math.max(rate * 1.5, ref * 2.2, 60).toFixed(0)
  );

  $('burn-rate-display').textContent = fmtRate(rate);
  $('projected-cost').textContent    = fmtUSD(proj);
  $('over-ref-badge').classList.toggle('visible', !!(win?.over_reference));

  if (win) {
    $('window-range').textContent =
      `${fmtDur(win.elapsed_seconds ?? 0)} elapsed · ${fmtDur(win.remaining_seconds ?? 0)} remaining`;
  }

  updateTitle();
}

// ---- burn history chart ----

function renderBurnHistory() {
  const series = S.active_window?.burn_rate_series;
  if (!series?.length) return;

  const ref = S.config?.reference_usd ?? 50;
  const xs  = series.map(d => new Date(d.t).getTime() / 1000);
  const ys  = series.map(d => d.usd_per_hour);

  _chart('burn-history-chart', {
    _key: ref,
    height: 60,
    cursor: { show: false },
    legend: { show: false },
    axes: [
      { stroke: CV['--mute'], ticks: { show: false }, grid: { show: false }, size: 24 },
      { stroke: CV['--mute'], ticks: { show: false }, grid: { stroke: CV['--rule'], width: 1 }, size: 34 },
    ],
    series: [
      {},
      { stroke: CV['--amber'], width: 1.5, fill: `rgba(${AMBER_RGB},0.12)`, label: '$/hr' },
      { stroke: CV['--alarm'], width: 1,   label: 'ref' },
    ],
    scales: { y: { range: (_, _min, max) => [0, Math.max(max, ref) * 1.15] } },
  }, [xs, ys, xs.map(() => ref)]);
}

// ---- today ----

function renderToday() {
  const t = S.today;
  if (!t) return;
  _todayTimestamp = Date.now();
  $('panel-today')?.classList.remove('stale');
  $('today-cost').setAttribute('value', (t.cost_usd ?? 0).toFixed(4));
  $('today-turns').textContent       = t.turns ?? 0;
  $('today-output').textContent      = fmtTok(t.output_tokens);
  $('today-cost-per-kw').textContent = t.cost_per_kw > 0 ? fmtUSD(t.cost_per_kw) : '–';
  $('today-hit-rate').textContent    = fmtPct(t.hit_rate);
}

// ---- hourly bars ----

function renderHourlyBars() {
  const hourly = S.today?.hourly;
  if (!hourly?.length) return;

  // hour field is an ISO datetime string like "2026-04-20T14:00:00+00:00"
  const byHour = {};
  for (const h of hourly) {
    const hr = parseInt((h.hour.split('T')[1] ?? h.hour).slice(0, 2));
    byHour[hr] = h;
  }

  const maxCost = Math.max(...Object.values(byHour).map(h => h.cost_usd), 0.0001);
  const nowHour = new Date().getUTCHours();
  const cont = $('hourly-bars');
  cont.innerHTML = '';

  for (let hr = 0; hr < 24; hr++) {
    const h = byHour[hr] ?? { cost_usd: 0, turns: 0 };
    const pct = Math.max(h.cost_usd / maxCost * 100, h.cost_usd > 0 ? 4 : 2);
    const bar = document.createElement('div');
    bar.className = 'hour-bar' + (hr === nowHour ? ' current' : '');
    bar.style.height = `${pct}%`;
    bar.title = `${String(hr).padStart(2, '0')}:00 — ${fmtUSD(h.cost_usd)} · ${h.turns} turns`;
    cont.appendChild(bar);
  }
}

// ---- daily area (stats + charts together) ----

function renderDailyArea() {
  const daily = S.daily_90d;
  if (!daily?.length) return;

  const cutDate = new Date();
  cutDate.setUTCDate(cutDate.getUTCDate() - prefs.dailyRange + 1);
  const cutStr = cutDate.toISOString().slice(0, 10);
  const slice  = daily.filter(d => d.date >= cutStr);

  if (!slice.length) return;

  const total    = slice.reduce((s, d) => s + d.cost_usd, 0);
  const bestDay  = slice.reduce((a, d) => d.cost_usd < a.cost_usd ? d : a);
  const worstDay = slice.reduce((a, d) => d.cost_usd > a.cost_usd ? d : a);

  $('daily-range-label').textContent = prefs.dailyRange;
  $('daily-total').textContent  = fmtUSD(total);
  $('daily-best').textContent   = `${fmtDate(bestDay.date)}  ${fmtUSD(bestDay.cost_usd)}`;
  $('daily-worst').textContent  = `${fmtDate(worstDay.date)} ${fmtUSD(worstDay.cost_usd)}`;

  const xs  = slice.map(d => new Date(d.date + 'T00:00:00Z').getTime() / 1000);
  const ys  = slice.map(d => d.cost_usd);
  const kws = slice.map(d => d.cost_per_kw);

  _chart('daily-chart', {
    height: 70,
    cursor: { show: false }, legend: { show: false },
    axes: [
      { stroke: CV['--mute'], ticks: { show: false }, grid: { show: false }, size: 24 },
      { stroke: CV['--mute'], ticks: { show: false }, grid: { stroke: CV['--rule'], width: 1 }, size: 34 },
    ],
    series: [{}, { stroke: CV['--amber'], width: 1.5, fill: `rgba(${AMBER_RGB},0.14)`, label: 'cost' }],
  }, [xs, ys]);

  _chart('kw-drift-chart', {
    height: 50,
    cursor: { show: false }, legend: { show: false },
    axes: [
      { stroke: CV['--mute'], ticks: { show: false }, grid: { show: false }, size: 24 },
      { stroke: CV['--mute'], ticks: { show: false }, grid: { stroke: CV['--rule'], width: 1 }, size: 34 },
    ],
    series: [{}, { stroke: CV['--cool'], width: 1.5, label: '$/kW' }],
  }, [xs, kws]);
}

// ---- sessions table ----

function renderSessions() {
  const sessions = S.sessions?.[prefs.sessionsRange] ?? [];
  $('sessions-tbody').innerHTML = sessions.map(s => {
    const vc = `verdict-${s.verdict}`;
    return `<tr onclick="location.href='/session/${s.id}'" style="cursor:pointer">
      <td title="${s.cwd ?? ''}">${s.id.slice(0, 8)}</td>
      <td>${shortModel(s.model)}</td>
      <td>${fmtUSD(s.cost_usd)}</td>
      <td>${s.turns}</td>
      <td>${fmtTok(s.max_input)}</td>
      <td><span class="verdict-pill ${vc}">${s.verdict}</span></td>
    </tr>`;
  }).join('');
}

// ---- models bars ----

function renderModels() {
  const models = S.models?.[prefs.modelsRange] ?? [];
  const total  = models.reduce((s, m) => s + m.cost_usd, 0) || 1;
  $('models-bars').innerHTML = models.map(m => {
    const pct   = (m.cost_usd / total * 100).toFixed(1);
    const color = modelColor(m.model);
    return `<div class="model-bar-row">
      <span class="model-name" title="${m.model}">${shortModel(m.model)}</span>
      <div class="model-bar-track">
        <div class="model-bar-fill" style="width:${pct}%;background:${color}"></div>
      </div>
      <span class="model-pct">${pct}%</span>
    </div>`;
  }).join('');
}

// ---- projects table ----

function renderProjects() {
  const projects = S.projects?.[prefs.projectsRange] ?? [];
  $('projects-tbody').innerHTML = projects.map(p => {
    const dir = p.cwd?.split('/').pop() || p.cwd || '–';
    return `<tr>
      <td title="${p.cwd ?? ''}">${dir}</td>
      <td>${fmtUSD(p.cost_usd)}</td>
      <td>${p.sessions}</td>
      <td>${fmtPct(p.cache_reuse_ratio)}</td>
    </tr>`;
  }).join('');
}

// ---- heatmap ----

function renderHeatmap() {
  const hm = S.heatmap_14d;
  if (!hm) return;

  // Hour label row (one blank + 24 hour labels)
  $('heatmap-hour-labels').innerHTML = '<span></span>' +
    Array.from({ length: 24 }, (_, h) => {
      const vis = [0, 4, 8, 12, 16, 20].includes(h) ? '' : 'style="visibility:hidden"';
      return `<span ${vis}>${String(h).padStart(2, '0')}</span>`;
    }).join('');

  const maxCost = Math.max(...hm.cells.flat(), 0.0001);
  const grid = $('heatmap-grid');
  grid.innerHTML = '';

  hm.dates.forEach((date, di) => {
    const lbl = document.createElement('div');
    lbl.className = 'hm-row-label';
    lbl.textContent = date.slice(5);
    grid.appendChild(lbl);

    hm.hours.forEach((h, hi) => {
      const cost = hm.cells[di][hi];
      const cell = document.createElement('div');
      cell.className = 'hm-cell';
      cell.style.background = hmColor(cost / maxCost);
      if (cost > 0) {
        cell.setAttribute('data-tip',
          `${date} ${String(h).padStart(2, '0')}:00 — ${fmtUSD(cost)}`);
      }
      grid.appendChild(cell);
    });
  });
}

// ---- live feed ----

function buildFeedItem(t) {
  const d   = new Date(t.ts);
  const hms = [d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds()]
                .map(n => String(n).padStart(2, '0')).join(':');
  const li  = document.createElement('li');
  li.dataset.key = `${t.session_id}|${t.ts}`;
  li.innerHTML = `<span class="feed-ts">${hms}</span>` +
    `<span class="feed-model">${shortModel(t.model)}</span>` +
    `<span class="feed-sess">${t.session_id.slice(0, 8)}</span>` +
    `<span class="feed-cost">${fmtUSD(t.cost_usd)}</span>` +
    `<span class="feed-meta">${fmtTok(t.output_tokens)} out ` +
    `${t.is_sidechain ? '<span class="sidechain-pill">sc</span>' : ''}` +
    `${t.tool_use_count > 0 ? `<span>${t.tool_use_count}t</span>` : ''}</span>`;
  return li;
}

function renderFeed() {
  const turns = S.recent_turns;
  const list  = $('live-feed-list');

  if (!turns?.length) {
    $('feed-idle-msg').classList.add('visible');
    list.innerHTML = '';
    return;
  }
  $('feed-idle-msg').classList.remove('visible');

  const items   = turns.filter(t => !prefs.hideSidechain || !t.is_sidechain).slice(0, 20);
  const liveKeys = new Set(items.map(t => `${t.session_id}|${t.ts}`));

  // remove stale rows
  [...list.children].forEach(li => { if (!liveKeys.has(li.dataset.key)) li.remove(); });

  // prepend new rows (items are newest-first; iterate reversed so prepend yields correct order)
  const existing = new Set([...list.children].map(li => li.dataset.key));
  for (let i = items.length - 1; i >= 0; i--) {
    const t = items[i];
    const k = `${t.session_id}|${t.ts}`;
    if (!existing.has(k)) list.prepend(buildFeedItem(t));
  }

  // trim to 20
  while (list.children.length > 20) list.lastElementChild.remove();
}

// ---- uPlot chart helper ----

function _chart(id, opts, data) {
  const cont = $(id);
  if (!cont) return;
  const { _key: key, ...plotOpts } = opts;
  const w = cont.offsetWidth || 320;

  if (charts[id]) {
    const canUpdate = charts[id].series.length === data.length && chartMeta[id] === key;
    if (canUpdate) {
      try { charts[id].setData(data); return; } catch (_) {}
    }
    try { charts[id].destroy(); } catch (_) {}
    cont.innerHTML = '';
  }

  try {
    charts[id] = new uPlot({ width: w, padding: [4, 4, 0, 0], select: { show: false }, ...plotOpts }, data, cont);
    chartMeta[id] = key;
  } catch (e) { console.warn('uPlot', id, e); }
}

// ---- range selectors ----

function wireRange(selId, onChange) {
  $(selId)?.querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => {
      $(selId).querySelectorAll('button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      onChange(btn.dataset.r ?? btn.dataset.lb ?? btn.dataset.v);
      window.savePrefs?.();
    });
  });
}

function spmKey(r) { return r === '1' ? '24h' : r === '7' ? '7d' : '14d'; }

wireRange('burn-lookback', lb => {
  prefs.burnLookback = lb;
  renderGauge();
});
wireRange('sessions-range-sel', r => {
  prefs.sessionsRange = spmKey(r);
  renderSessions();
});
wireRange('models-range-sel', r => {
  prefs.modelsRange = spmKey(r);
  renderModels();
});
wireRange('projects-range-sel', r => {
  prefs.projectsRange = spmKey(r);
  renderProjects();
});
wireRange('daily-range-sel', r => {
  prefs.dailyRange = parseInt(r);
  renderDailyArea();
});

// ---- wall clock + stale-today detector ----

function updateClock() {
  $('wall-clock').textContent = new Date().toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZoneName: 'short',
  });
  if (_todayTimestamp && Date.now() - _todayTimestamp > 300_000) {
    $('panel-today')?.classList.add('stale');
  }
}
updateClock();
setInterval(updateClock, 1000);

// ---- title bar ----

function updateTitle() {
  const win  = S.active_window;
  const ref  = S.config?.reference_usd ?? 50;
  const rate = win?.[`burn_rate_usd_per_hour_${prefs.burnLookback}`] ?? 0;
  if (win?.over_reference)   document.title = `tokenol — ⚠ over $${ref}`;
  else if (rate > 0.001)     document.title = `tokenol — ${fmtRate(rate)}`;
  else                       document.title = 'tokenol — live';
}

// ---- idle / dead-feed detection ----

function _setIdle(idle) {
  $('panel-feed')?.classList.toggle('feed-idle', idle);
  if (idle) {
    $('feed-idle-msg').classList.add('visible');
    $('live-feed-list').innerHTML = '';
    document.title = 'tokenol — IDLE';
  } else {
    updateTitle();
  }
}

function resetIdleTimer() {
  clearTimeout(_idleTimer);
  _setIdle(false);
  _idleTimer = setTimeout(() => _setIdle(true), 30_000);
}

// ---- keyboard shortcuts ----

document.addEventListener('keydown', e => {
  if (e.target.matches('input,textarea')) return;
  if (e.key === 'g') $('panel-gauge')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (e.key === 'l') $('panel-feed')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (e.key === '/') { e.preventDefault(); $('sessions-table')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
});

// ---- SSE connection ----

const dot = $('conn-dot');
let es, _reconnectDelay = 1000;

function connect() {
  es = new EventSource('/api/stream');

  es.onopen = () => { dot.className = 'pulse'; _reconnectDelay = 1000; resetIdleTimer(); };

  es.onmessage = ev => {
    try { mergeState(JSON.parse(ev.data)); }
    catch (e) { console.error('SSE parse', e); }
  };

  es.onerror = () => {
    dot.className = 'alarm';
    es.close();
    setTimeout(connect, _reconnectDelay);
    _reconnectDelay = Math.min(_reconnectDelay * 2, 30000);
  };
}

connect();
