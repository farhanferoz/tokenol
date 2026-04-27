import { fmtUSD, fmtTok, fmtRelTime, fmtRatio, cwdBasename, shortModel, verdictPill, esc, GLOSSARY } from './components.js';
import { drawChart }                             from './chart.js';

// ---- DOM helpers ----
const $ = id => document.getElementById(id);

function _initGlossary() {
  const dl = $('glossary-list');
  if (dl) dl.innerHTML = GLOSSARY.map(e => `<dt>${esc(e.term)}</dt><dd>${esc(e.def)}</dd>`).join('');
  const byTerm = Object.fromEntries(GLOSSARY.map(e => [e.term, e.def]));
  // Tile labels map ids → terms; everything else tags itself with data-term.
  const _TILE_GLOSSARY_MAP = {
    'tile-hit-lbl':   'Hit% (cache hit rate)',
    'tile-cost-lbl':  '$/kW (cost per 1k out)',
    'tile-ctx-lbl':   'Ctx (context ratio)',
    'tile-cache-lbl': 'Cache reuse',
  };
  for (const [id, term] of Object.entries(_TILE_GLOSSARY_MAP)) {
    const el = $(id);
    if (el && byTerm[term]) el.title = byTerm[term];
  }
  // Stamp any element tagged with data-term.
  document.querySelectorAll('[data-term]').forEach(el => {
    const def = byTerm[el.dataset.term];
    if (def) el.title = def;
  });
}

function _wireModalBackdrops() {
  document.querySelectorAll('.modal-bg').forEach(bg => {
    bg.addEventListener('click', e => {
      if (e.target.closest('.modal-inner')) return;
      const id = [...bg.classList].find(c => c.startsWith('m-'));
      if (id) { const el = document.getElementById(id); if (el) el.checked = false; }
    });
  });
}

const _toLocalDate = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;

// ---- period state ----
const _LS_PERIOD     = 'tokenol.prefs.period';
const _VALID_PERIODS = new Set(['today', '7d', '30d', 'all']);

function _getPeriod() {
  const v = localStorage.getItem(_LS_PERIOD);
  return _VALID_PERIODS.has(v) ? v : 'today';
}
function _setPeriod(p) { localStorage.setItem(_LS_PERIOD, p); }

// ---- SSE dot ----
const _dot = $('sse-dot');
let _dotBaseTitle = '';
function _dotState(cls, title) {
  _dot.className = 'sse-dot' + (cls ? ' ' + cls : '');
  _dotBaseTitle = title;
  _dot.title = title;
}
// Live tooltip: "Live — connected · last update 5s ago" — updated every second
// so you can hover-and-tell whether the page is genuinely stale.
setInterval(() => {
  if (!_dot) return;
  if (_lastMsgAt) {
    const ageS = Math.round((Date.now() - _lastMsgAt) / 1000);
    _dot.title = `${_dotBaseTitle} · last update ${ageS}s ago`;
  } else {
    _dot.title = _dotBaseTitle;
  }
}, 1_000);

// ---- SSE connection ----
let _es = null;
let _reconnectDelay = 1_000;
let _fiveXXSince    = null;
let _lastMsgAt      = 0;
// Server heartbeat is ≤60s; >90s without a message means the connection
// silently stalled (system sleep, NAT drop, transparent proxy).
const _STALE_MS = 90_000;

function _scheduleReconnect(reason) {
  if (_es) { _es.close(); _es = null; }
  _lastMsgAt = 0;
  _dotState('amber', `SSE ${reason} — reconnecting…`);
  setTimeout(() => _connect(_getPeriod()), _reconnectDelay);
  _reconnectDelay = Math.min(_reconnectDelay * 2, 30_000);
}

function _connect(period) {
  if (_es) { _es.close(); _es = null; }
  _lastMsgAt = 0;
  _dotState('', 'SSE connecting…');
  _es = new EventSource(`/api/stream?period=${period}`);

  _es.onopen = () => {
    _dotState('connected', 'Live — connected');
    _reconnectDelay = 1_000;
    _fiveXXSince    = null;
    _lastMsgAt      = Date.now();
    _resetIdleTimer();
  };

  _es.onmessage = ev => {
    _lastMsgAt = Date.now();
    try { _applyPayload(JSON.parse(ev.data)); }
    catch (e) { console.error('SSE parse', e); }
  };

  _es.onerror = () => _scheduleReconnect('disconnected');
}

// Watchdog: EventSource doesn't reliably fire onerror on silent stalls.
setInterval(() => {
  if (!_es || !_lastMsgAt) return;
  if (Date.now() - _lastMsgAt > _STALE_MS) _scheduleReconnect('stalled');
}, 10_000);

// Browsers throttle background tabs — timers slow, SSE may pause. On tab
// return, if our last message is older than one tick + slack, force a fresh
// reconnect so the user never sees stale data.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  if (!_lastMsgAt || Date.now() - _lastMsgAt > 15_000) {
    _reconnectDelay = 1_000;
    _scheduleReconnect('tab visible');
  }
});

// Backstop: re-fetch the authoritative snapshot periodically. SSE is the
// fast path; this catches any case where SSE flows but the merged client
// state drifts (browser extension hooks, long-lived tab quirks, etc.).
// Skipped while hidden — visibilitychange handler covers the wakeup case.
setInterval(() => {
  if (document.visibilityState !== 'visible') return;
  _fetchSnapshot(_getPeriod());
}, 30_000);

// Debug: window.__tokenolForceReconnect() simulates a silent stall.
window.__tokenolForceReconnect = () => _scheduleReconnect('forced (debug)');

// ---- initial fetch ----
async function _fetchSnapshot(period) {
  try {
    const res = await fetch(`/api/snapshot?period=${period}`);
    if (res.ok) {
      _fiveXXSince = null;
      _applyPayload(await res.json());
    } else if (res.status >= 500) {
      if (_fiveXXSince === null) _fiveXXSince = Date.now();
      if (Date.now() - _fiveXXSince > 30_000) _dotState('alarm', 'Server error — check logs');
    }
  } catch (_e) { /* network error; SSE will reconnect */ }
}

// ---- state + render ----
let S = {};

function _applyPayload(payload) {
  // SSE delivers a full payload first, then shallow diffs (only changed top-level keys).
  // Merge so stable keys (e.g. projects_30d, models_30d) survive across ticks.
  S = { ...S, ...payload };
  _resetIdleTimer();
  _render();
}

function _render() {
  _renderTopbar(S);
  _renderTiles(S);
  _renderAnomalyStrip(S);
  _renderHourly(S);
  _renderDaily(S);
  _renderModels(S);
  _renderRecent(S);
  _loadSettings(S);
}

// ---- topbar summary ----
const _topbarEl = $('topbar-summary');
let   _topbarHtml = '';

function _renderTopbar(payload) {
  const ts = payload.topbar_summary;
  if (!ts || !_topbarEl) return;
  const parts = [];
  if (ts.today_cost     != null) parts.push(`<span class="k">cost</span> <span class="v">${fmtUSD(ts.today_cost)}</span>`);
  if (ts.sessions_count != null) parts.push(`<span class="k">sessions</span> <span class="v">${ts.sessions_count}</span>`);
  if (ts.output_tokens  != null) parts.push(`<span class="k">output</span> <span class="v">${fmtTok(ts.output_tokens)}</span>`);
  if (ts.last_active) parts.push(`<span class="k">last active</span> <span class="v">${fmtRelTime(ts.last_active)}</span>`);
  const html = parts.join('<span class="sep">·</span>');
  if (html === _topbarHtml) return;
  _topbarHtml = html;
  _topbarEl.innerHTML = html;
}

// ---- efficiency tiles ----
const _TILE_CFG = {
  hit_pct: {
    dom:        'hit',
    fmt:        v => `${(+v || 0).toFixed(1)}%`,
    higherGood: true,
    colour:     (v, g) => v == null ? 'mute' : v >= g.good_gte ? 'good' : v < g.red_lt  ? 'alarm' : 'warn',
    goalText:   g => `≥${g.good_gte}% good · <${g.red_lt}% red`,
  },
  cost_per_kw: {
    dom:        'cost',
    fmt:        fmtUSD,
    higherGood: false,
    colour:     (v, g) => v == null ? 'mute' : v <= g.good_lte ? 'good' : v > g.red_gt  ? 'alarm' : 'warn',
    goalText:   g => `<${fmtUSD(g.good_lte)} good · >${fmtUSD(g.red_gt)} red`,
  },
  ctx_ratio: {
    dom:        'ctx',
    fmt:        fmtRatio,
    higherGood: false,
    colour:     (v, g) => v == null ? 'mute' : v > g.red_gt ? 'alarm' : 'mute',
    goalText:   g => `<${Math.round(g.red_gt)}:1 ok`,
  },
  cache_reuse: {
    dom:        'cache',
    fmt:        fmtRatio,
    higherGood: true,
    colour:     (v, g) => v == null ? 'mute' : v >= g.good_gte ? 'good' : v < g.red_lt ? 'alarm' : 'warn',
    goalText:   g => `≥${Math.round(g.good_gte)}:1 good · <${Math.round(g.red_lt)}:1 red`,
  },
};

// Cache DOM refs once (module runs after DOM is ready)
for (const [, cfg] of Object.entries(_TILE_CFG)) {
  cfg.numEl    = $(`tile-${cfg.dom}-num`);
  cfg.dltEl    = $(`tile-${cfg.dom}-delta`);
  cfg.goalEl   = $(`tile-${cfg.dom}-goal`);
  cfg.recentEl = $(`tile-${cfg.dom}-recent`);
}

let _tileGoals = {};

function _renderTiles(payload) {
  const tiles = payload.tiles;
  if (!tiles) return;
  for (const [key, cfg] of Object.entries(_TILE_CFG)) {
    const tile = tiles[key];
    _tileGoals[key] = tile?.goal ?? {};
    if (!cfg.numEl) continue;
    const v = tile?.value;
    cfg.numEl.className   = `num ${cfg.colour(v, tile?.goal ?? {})}`;
    cfg.numEl.textContent = v != null ? cfg.fmt(v) : '—';
    if (cfg.dltEl) _setTileDelta(cfg.dltEl, tile, cfg.higherGood);
    if (cfg.goalEl && tile?.goal) cfg.goalEl.textContent = cfg.goalText(tile.goal);
    if (cfg.recentEl) {
      const lhv = tile?.last_hour_value;
      cfg.recentEl.textContent = lhv == null ? '' : `last hour: ${cfg.fmt(lhv)}`;
    }
  }
}

function _setTileDelta(el, tile, higherGood) {
  if (!tile) { el.className = 'delta'; el.textContent = ''; return; }
  const { delta_ratio: dr, baseline_label: lbl } = tile;
  if (lbl === 'cold') {
    el.className   = 'cold';
    el.textContent = '— first day (need 3d for baseline)';
    return;
  }
  el.className = 'delta';
  if (dr == null) { el.textContent = lbl ? `vs ${lbl} median` : ''; return; }
  const pct = Math.round((dr - 1) * 100);
  const abs = Math.abs(pct);
  el.textContent = `${pct >= 0 ? '↑' : '↓'}${abs}% vs ${lbl} median`;
  if (abs >= 20) el.className = `delta ${(higherGood ? pct > 0 : pct < 0) ? 'good' : 'alarm'}`;
}

// ---- anomaly strip ----
const _anomalyEl    = $('anomaly-strip');
let   _lastAnomalyKey = null;

function _renderAnomalyStrip(payload) {
  if (!_anomalyEl) return;
  const a   = payload.anomaly;
  const key = a ? `${a.severity}:${a.message}` : null;
  if (key === _lastAnomalyKey) return;
  _lastAnomalyKey = key;
  if (!a) { _anomalyEl.innerHTML = ''; return; }
  const isRed = a.severity === 'red';
  const div   = document.createElement('div');
  div.className = `anomaly${isRed ? ' red' : ''} fade-in`;
  div.setAttribute('role', 'alert');
  const glyph = document.createElement('span'); glyph.className = 'glyph'; glyph.textContent = isRed ? '●' : '⚠';
  const msg   = document.createElement('span'); msg.className   = 'msg';   msg.textContent   = a.message;
  const arrow = document.createElement('span'); arrow.className = 'arrow'; arrow.textContent = 'inspect →';
  div.append(glyph, msg, arrow);
  if (a.drilldown_href) div.addEventListener('click', () => { location.href = a.drilldown_href; });
  _anomalyEl.replaceChildren(div);
}

// ---- filter state ----
// Each filter holds { mode: 'all' | 'compare' | 'specific', selected: Set<string> }.
// Converted to an API param string via _filterParam().
function _newFilterState() { return { mode: 'all', selected: new Set() }; }

function _filterParam(fs) {
  if (fs.mode === 'specific' && fs.selected.size) return [...fs.selected].join(',');
  return 'all';
}

function _filterLabel(fs, items, noun) {
  if (fs.mode === 'all') return 'all';
  if (fs.selected.size === 1) {
    const only = [...fs.selected][0];
    const hit = items.find(it => it.value === only);
    return hit ? hit.label : _shortSeriesLabel(only);
  }
  return `${fs.selected.size} ${noun}`;
}

function _loadFilter(key) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return _newFilterState();
    const p = JSON.parse(raw);
    // Back-compat: older builds stored mode='compare'; collapse it to 'all'.
    const mode = (p.mode === 'all' || p.mode === 'specific') ? p.mode : 'all';
    return { mode, selected: new Set(p.selected ?? []) };
  } catch { return _newFilterState(); }
}

function _saveFilter(key, fs) {
  localStorage.setItem(key, JSON.stringify({ mode: fs.mode, selected: [...fs.selected] }));
}

// Each panel owns its own project/model filter state.
const _hProjFs  = _loadFilter('tokenol.filter.hourly.project');
const _hModelFs = _loadFilter('tokenol.filter.hourly.model');
const _dProjFs  = _loadFilter('tokenol.filter.daily.project');
const _dModelFs = _loadFilter('tokenol.filter.daily.model');

// ---- shared chart-data builders ----
// Produce a chart-data bundle from an array of snapshot points keyed by time.
function _buildSnapshotData({ points, field, yUnit, timeOf, overlay }) {
  if (!points.length) return { xs: new Float64Array(0), ySeries: [], labels: [], yUnit };
  const xs      = new Float64Array(points.map(timeOf));
  const ySeries = [new Float64Array(points.map(p => p[field] ?? NaN))];
  const labels  = ['all'];
  const turnsByX = new Map(points.map((p, i) => [xs[i], p.turns ?? 0]));
  if (overlay?.points?.length) {
    const lookup = Object.fromEntries(overlay.points.map(p => [p[overlay.keyField], p.value]));
    ySeries.push(new Float64Array(points.map(p => lookup[p[overlay.keyField]] ?? NaN)));
    labels.push(overlay.label);
  }
  return { xs, ySeries, labels, yUnit, turnsByX };
}

function _shortSeriesLabel(lbl) {
  if (typeof lbl !== 'string') return lbl;
  if (lbl.startsWith('/'))       return cwdBasename(lbl);
  if (lbl.startsWith('claude-')) return shortModel(lbl);
  return lbl;
}

function _normApiSeries(d, keyOf, tsOf, cmp) {
  const keys = [...new Set(d.series.flatMap(s => s.points.map(keyOf)))].sort(cmp);
  const xs = new Float64Array(keys.map(tsOf));
  const turnsByX = new Map();
  keys.forEach((k, i) => {
    const total = d.series.reduce((sum, s) => {
      const pt = s.points.find(p => keyOf(p) === k);
      return sum + (pt?.turns ?? 0);
    }, 0);
    turnsByX.set(xs[i], total);
  });
  return {
    xs,
    ySeries: d.series.map(s => {
      const byK = Object.fromEntries(s.points.map(p => [keyOf(p), p.value]));
      return new Float64Array(keys.map(k => byK[k] ?? NaN));
    }),
    labels:  d.series.map(s => _shortSeriesLabel(s.label)),
    yUnit:   d.y_unit,
    turnsByX,
  };
}

// Paint a timeline chart into a container, rendering an empty-state placeholder when
// data is absent. Shared by both hourly and daily panels.
function _paintTimeline(containerId, data, emptyText, drawOpts) {
  const cont = $(containerId);
  if (!cont) return;
  if (!data.xs.length) {
    if (cont._uplot) { cont._uplot.destroy(); cont._uplot = null; }
    cont.innerHTML = `<div class="tl-insufficient">${emptyText}</div>`;
    return;
  }
  drawChart(cont, { ...data, ...drawOpts });
}

// Guarded async fetcher: skips when already in-flight, paints on 2xx.
function _makeFetcher({ urlFn, xform, paint }) {
  let busy = false;
  return async () => {
    if (busy) return;
    busy = true;
    try {
      const res = await fetch(urlFn());
      if (res.ok) paint(xform(await res.json()));
    } finally { busy = false; }
  };
}

// ---- y-scale (linear / log) per panel, persisted ----
const _LS_H_SCALE = 'tokenol.hourly.scale';
const _LS_D_SCALE = 'tokenol.daily.scale';
const _scaleFor = (metric, stored) => {
  // Hit% is bounded [0,1], log would be meaningless there — force linear.
  if (metric === 'hit_pct') return 'linear';
  return stored === 'log' ? 'log' : 'linear';
};
function _syncScalePills(groupId, active) {
  const group = $(groupId);
  if (!group) return;
  group.querySelectorAll('[data-scale]').forEach(el => {
    el.classList.toggle('on', el.dataset.scale === active);
  });
  // Disable the log pill entirely when the active metric forces linear
  const logPill = group.querySelector('[data-scale="log"]');
  if (logPill) logPill.classList.toggle('disabled', active === 'linear-forced');
}

// ---- hourly timeline ----
let _hMetric    = 'hit_pct';
let _hScaleRaw  = localStorage.getItem(_LS_H_SCALE) || 'linear';
let _hDay       = null;   // null = today
let _hEarliest  = null;

function _snapshotToChartData(ht) {
  return _buildSnapshotData({
    points: ht.series,
    field:  'hit_pct',
    yUnit:  'percent',
    timeOf: p => new Date(p.hour).getTime() / 1000,
  });
}

function _apiToChartData(d) {
  const chart = _normApiSeries(d, p => p.hour, iso => new Date(iso).getTime() / 1000, (a, b) => a < b ? -1 : a > b ? 1 : 0);
  chart._activeProjects = d.active_projects ?? [];
  chart._activeModels   = d.active_models   ?? [];
  return chart;
}

function _renderHourly(payload) {
  const ht = payload.hourly_today;
  if (!ht) return;
  if (_hEarliest === null) _hEarliest = ht.earliest_available;
  const canUseSnapshot = _hMetric === 'hit_pct' && _hDay === null
    && _hProjFs.mode === 'all' && _hModelFs.mode === 'all';
  if (canUseSnapshot) {
    // Snapshot is today-scoped → its active lists are today's cwds/models.
    _hActiveProjects = ht.active_projects ?? [];
    _hActiveModels   = ht.active_models   ?? [];
    _paintHourly(_snapshotToChartData(ht));
  } else {
    _fetchHourly();
  }
}

const _fetchHourly = _makeFetcher({
  urlFn: () => {
    const day = _hDay ?? _toLocalDate(new Date());
    const p = new URLSearchParams({
      metric: _hMetric,
      project: _filterParam(_hProjFs),
      model:   _filterParam(_hModelFs),
    });
    return `/api/hourly/${day}?${p}`;
  },
  xform: _apiToChartData,
  paint: data => {
    _hActiveProjects = data._activeProjects ?? _hActiveProjects;
    _hActiveModels   = data._activeModels   ?? _hActiveModels;
    _paintHourly(data);
  },
});

function _paintHourly(data) {
  const scale = _scaleFor(_hMetric, _hScaleRaw);
  _syncScalePills('hourly-scale-pills', _hMetric === 'hit_pct' ? 'linear-forced' : scale);
  _paintTimeline('hourly-chart', data, 'No data for this selection.', {
    stepped: true,
    yScale: scale,
    onPointClick: ts => {
      const d = new Date(ts * 1000);
      location.href = `/day/${_toLocalDate(d)}#hour=${d.getHours()}`;
    },
  });
}

// ---- dropdown ----
let _activeDropdown = null;

function _closeDropdown() {
  if (_activeDropdown) { _activeDropdown.remove(); _activeDropdown = null; }
}

document.addEventListener('click', e => {
  if (_activeDropdown && !_activeDropdown.contains(e.target)) _closeDropdown();
});

function _pickDropdown(anchor, items, onPick) {
  _closeDropdown();
  const rect = anchor.getBoundingClientRect();
  const dd   = document.createElement('div');
  dd.className = 'tl-dropdown';
  dd.style.top  = `${rect.bottom + window.scrollY + 4}px`;
  dd.style.left = `${rect.left  + window.scrollX}px`;
  items.forEach(({ label, value }) => {
    const li = document.createElement('div');
    li.className = 'tl-dd-item';
    li.textContent = label;
    li.addEventListener('click', () => { _closeDropdown(); onPick(value, label); });
    dd.appendChild(li);
  });
  document.body.appendChild(dd);
  _activeDropdown = dd;
}

// Multi-select dropdown: 'all' radio + individual checkbox list. Commits via onChange.
function _pickMultiDropdown(anchor, { mode, selected, items, noun, onChange }) {
  _closeDropdown();
  const rect = anchor.getBoundingClientRect();
  const dd   = document.createElement('div');
  dd.className = 'tl-dropdown tl-dropdown-multi';
  dd.style.top  = `${rect.bottom + window.scrollY + 4}px`;
  dd.style.left = `${rect.left  + window.scrollX}px`;

  const emit = () => onChange({ mode, selected: new Set(selected) });

  // 'all' radio resets everything.
  const allRow = document.createElement('label');
  allRow.className = 'tl-dd-mode';
  allRow.innerHTML = `<input type="radio" name="ddmode"${mode === 'all' ? ' checked' : ''}><span>all</span>`;
  allRow.querySelector('input').addEventListener('change', () => {
    mode = 'all';
    selected.clear();
    dd.querySelectorAll('.tl-dd-check input').forEach(cb => { cb.checked = false; });
    emit();
  });
  dd.appendChild(allRow);

  // Ensure currently-selected values appear as togglable rows even if they're
  // no longer present in the 30d items list.
  const itemValues = new Set(items.map(it => it.value));
  const stragglers = [...selected]
    .filter(v => !itemValues.has(v))
    .map(v => ({ label: _shortSeriesLabel(v), value: v }));
  const allItems = [...stragglers, ...items];

  const sep = document.createElement('div');
  sep.className = 'tl-dd-sep';
  sep.textContent = `pick specific ${noun}`;
  dd.appendChild(sep);

  if (!allItems.length) {
    const empty = document.createElement('div');
    empty.className = 'tl-dd-empty';
    empty.textContent = `no ${noun} active in this view`;
    dd.appendChild(empty);
    document.body.appendChild(dd);
    _activeDropdown = dd;
    return;
  }

  const listEl = document.createElement('div');
  listEl.className = 'tl-dd-list';
  allItems.forEach(({ label, value }) => {
    const row = document.createElement('label');
    row.className = 'tl-dd-check';
    row.innerHTML = `<input type="checkbox"${selected.has(value) ? ' checked' : ''}><span>${esc(label)}</span>`;
    row.querySelector('input').addEventListener('change', e => {
      if (e.target.checked) selected.add(value);
      else                  selected.delete(value);
      if (selected.size > 0) {
        mode = 'specific';
        allRow.querySelector('input').checked = false;
      } else {
        mode = 'all';
        allRow.querySelector('input').checked = true;
      }
      emit();
    });
    listEl.appendChild(row);
  });
  dd.appendChild(listEl);

  document.body.appendChild(dd);
  _activeDropdown = dd;
}

function _dayItems() {
  const items = [{ label: 'today', value: null }];
  if (_hEarliest) {
    const d = new Date();
    for (let i = 1; i < 30; i++) {
      d.setDate(d.getDate() - 1);
      const iso = _toLocalDate(d);
      if (iso < _hEarliest) break;
      items.push({ label: iso, value: iso });
    }
  }
  return items;
}

// Bind a single button to a filter state. One button → one state → one fetch.
function _wireMultiFilter(btn, { storageKey, noun, state, getItems, fetchFn }) {
  if (!btn) return;
  const refresh = () => { btn.textContent = _filterLabel(state, getItems(), noun) + ' ▾'; };
  refresh();
  btn.addEventListener('click', e => {
    e.stopPropagation();
    _pickMultiDropdown(btn, {
      mode: state.mode,
      selected: state.selected,
      items: getItems(),
      noun,
      onChange: next => {
        state.mode = next.mode;
        state.selected = next.selected;
        _saveFilter(storageKey, state);
        refresh();
        fetchFn();
      },
    });
  });
}

// Per-panel active lists. Each panel keeps its own list matching the panel's
// current view (hourly = today's turns; daily = range's turns). Updated every
// time the panel paints (from snapshot or from a fetch response).
let _hActiveProjects = [];
let _hActiveModels   = [];
let _dActiveProjects = [];
let _dActiveModels   = [];

const _hProjectItems = () => _hActiveProjects;
const _hModelItems   = () => _hActiveModels;
const _dProjectItems = () => _dActiveProjects;
const _dModelItems   = () => _dActiveModels;

function _initTzLabel() {
  const tz = new Date().toLocaleTimeString(undefined, { timeZoneName: 'short' }).split(' ').pop();
  document.querySelectorAll('.tl-y-lbl').forEach(el => {
    if (tz && !el.textContent.includes(tz)) el.textContent = `${el.textContent} · times ${tz}`;
  });
}

function _wireHourly() {
  _wireRange('hourly-metric-pills', m => { _hMetric = m; _fetchHourly(); });
  _wireRange('hourly-scale-pills',  s => {
    _hScaleRaw = s;
    localStorage.setItem(_LS_H_SCALE, s);
    _fetchHourly();
  });

  const dayBtn = $('hourly-day-picker');
  dayBtn?.addEventListener('click', e => {
    e.stopPropagation();
    _pickDropdown(dayBtn, _dayItems(), v => {
      _hDay = v;
      dayBtn.textContent = v ?? 'today ▾';
      _fetchHourly();
    });
  });

  _wireMultiFilter($('hourly-project-filter'), {
    storageKey: 'tokenol.filter.hourly.project', noun: 'projects',
    state: _hProjFs, getItems: _hProjectItems, fetchFn: _fetchHourly,
  });
  _wireMultiFilter($('hourly-model-filter'), {
    storageKey: 'tokenol.filter.hourly.model', noun: 'models',
    state: _hModelFs, getItems: _hModelItems, fetchFn: _fetchHourly,
  });
}

// ---- daily timeline ----
const _DAILY_META = {
  hit_pct:    { field: 'hit_pct',       yUnit: 'percent' },
  cost_per_kw:{ field: 'cost_per_kw',   yUnit: 'usd' },
  ctx_ratio:  { field: 'ctx_ratio',     yUnit: 'ratio' },
  cache_reuse:{ field: 'cache_reuse',   yUnit: 'ratio' },
  output:     { field: 'output_tokens', yUnit: 'tokens' },
  cost:       { field: 'cost_usd',      yUnit: 'usd' },
};

let _dMetric   = 'hit_pct';
let _dRange    = '30d';
let _dEarliest = null;
let _dScaleRaw = localStorage.getItem(_LS_D_SCALE) || 'linear';

const _xFmtDate = v => { const d = new Date(v * 1000); return `${d.getMonth()+1}/${d.getDate()}`; };

function _snapshotToDailyData(daily, metric) {
  const { field, yUnit } = _DAILY_META[metric] ?? { field: metric, yUnit: 'usd' };
  return _buildSnapshotData({
    points: daily.series,
    field,
    yUnit,
    timeOf: p => new Date(p.date + 'T00:00:00').getTime() / 1000,
    overlay: metric === 'hit_pct' && daily.moving_avg_7d?.length
      ? { points: daily.moving_avg_7d, keyField: 'date', label: '7d avg' }
      : null,
  });
}

function _apiToDailyChartData(d) {
  const chart = _normApiSeries(d, p => p.date, dt => new Date(dt + 'T00:00:00').getTime() / 1000);
  chart._activeProjects = d.active_projects ?? [];
  chart._activeModels   = d.active_models   ?? [];
  chart._note           = d.note ?? null;
  return chart;
}

const _RANGE_DISABLE_DAYS = { '30d': 7, '90d': 30 };

function _updateDailyRangePills() {
  const group = $('daily-range-pills');
  if (!group || !_dEarliest) return;
  const daysAvail = Math.floor((Date.now() - new Date(_dEarliest)) / 86_400_000);
  group.querySelectorAll('[data-range]').forEach(btn => {
    const threshold = _RANGE_DISABLE_DAYS[btn.dataset.range];
    btn.classList.toggle('disabled', threshold != null && daysAvail <= threshold);
  });
}

function _renderDaily(payload) {
  const daily = payload.daily;
  if (!daily) return;
  if (daily.earliest_available && daily.earliest_available !== _dEarliest) {
    _dEarliest = daily.earliest_available;
    _updateDailyRangePills();
  }
  const canUseSnapshot = _dMetric === 'hit_pct' && _dRange === '30d'
    && _dProjFs.mode === 'all' && _dModelFs.mode === 'all';
  if (canUseSnapshot) {
    // Snapshot daily is 30-day-scoped → its active lists are last-30d cwds/models.
    _dActiveProjects = daily.active_projects ?? [];
    _dActiveModels   = daily.active_models   ?? [];
    _paintDaily(_snapshotToDailyData(daily, _dMetric));
  } else {
    _fetchDaily();
  }
}

const _fetchDaily = _makeFetcher({
  urlFn: () => {
    const p = new URLSearchParams({
      range: _dRange, metric: _dMetric,
      project: _filterParam(_dProjFs),
      model:   _filterParam(_dModelFs),
    });
    return `/api/daily?${p}`;
  },
  xform: _apiToDailyChartData,
  paint: data => {
    _dActiveProjects = data._activeProjects ?? _dActiveProjects;
    _dActiveModels   = data._activeModels   ?? _dActiveModels;
    _paintDaily(data);
  },
});

function _paintDaily(data) {
  const scale = _scaleFor(_dMetric, _dScaleRaw);
  _syncScalePills('daily-scale-pills', _dMetric === 'hit_pct' ? 'linear-forced' : scale);
  const note = $('daily-note');
  if (note) {
    if (data._note) { note.textContent = data._note; note.classList.remove('hidden'); }
    else            { note.textContent = '';         note.classList.add('hidden');    }
  }
  _paintTimeline('daily-chart', data, 'No history yet — check back after a few days.', {
    xFmt: _xFmtDate,
    stepped: data.labels.map(lbl => lbl !== '7d avg'),
    dashes: [null, [4, 4]],
    yScale: scale,
    onPointClick: ts => { location.href = `/day/${_toLocalDate(new Date(ts * 1000))}`; },
  });
}

function _wireDaily() {
  _wireRange('daily-range-pills',  r => { _dRange  = r; _fetchDaily(); });
  _wireRange('daily-metric-pills', m => { _dMetric = m; _fetchDaily(); });
  _wireRange('daily-scale-pills',  s => {
    _dScaleRaw = s;
    localStorage.setItem(_LS_D_SCALE, s);
    _fetchDaily();
  });

  _wireMultiFilter($('daily-project-filter'), {
    storageKey: 'tokenol.filter.daily.project', noun: 'projects',
    state: _dProjFs, getItems: _dProjectItems, fetchFn: _fetchDaily,
  });
  _wireMultiFilter($('daily-model-filter'), {
    storageKey: 'tokenol.filter.daily.model', noun: 'models',
    state: _dModelFs, getItems: _dModelItems, fetchFn: _fetchDaily,
  });
}

// ---- period pills ----
function _wirePeriodPills() {
  const group = $('period-pills');
  if (!group) return;
  const current = _getPeriod();
  group.querySelectorAll('[data-period]').forEach(el => {
    el.classList.toggle('on', el.dataset.period === current);
    el.addEventListener('click', () => {
      const p = el.dataset.period;
      if (p === _getPeriod()) return;
      _setPeriod(p);
      group.querySelectorAll('[data-period]').forEach(b => b.classList.toggle('on', b.dataset.period === p));
      S = {};
      _render();
      _fetchSnapshot(p);
      _connect(p);
    });
  });
}

// ---- wall clock ----
const _clockEl = $('wall-clock');
function _updateClock() {
  if (_clockEl) _clockEl.textContent = new Date().toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZoneName: 'short',
  });
}
_updateClock();
setInterval(_updateClock, 1000);

// ---- idle detection ----
let _idleTimer = null;
let _isIdle    = false;

function _setIdle(idle) {
  if (_isIdle === idle) return;
  _isIdle = idle;
  if (!idle) document.title = 'tokenol — live';
}

function _resetIdleTimer() {
  clearTimeout(_idleTimer);
  _setIdle(false);
  _idleTimer = setTimeout(() => _setIdle(true), 30_000);
}

// ---- keyboard shortcuts ----
let _kbLastKey = null, _kbLastKeyTimer = null;
const _modalOpen  = id => { const el = $(id); if (el) el.checked = true; };
const _modalClose = id => { const el = $(id); if (el) el.checked = false; };
const _anyModalOpen = () => !!document.querySelector('.modal-toggle:checked');
const _closeAllModals = () => document.querySelectorAll('.modal-toggle').forEach(t => { t.checked = false; });

document.addEventListener('keydown', e => {
  const tag = e.target.tagName;
  const inInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'BUTTON' || tag === 'LABEL';

  if (e.key === 'Escape') {
    if (_anyModalOpen()) { _closeAllModals(); return; }
    if (!inInput && location.pathname !== '/') history.back();
    return;
  }

  if (!inInput) {
    if (e.key === '?') { _modalOpen('m-gl'); return; }
    if (e.key === '/') { e.preventDefault(); _modalOpen('m-search'); return; }
    if (e.key === ',') { _modalOpen('m-set'); return; }

    clearTimeout(_kbLastKeyTimer);
    if (_kbLastKey === 'g' && e.key === 't') {
      window.scrollTo({ top: 0, behavior: 'smooth' });
      _kbLastKey = null; return;
    }
    _kbLastKey = e.key;
    _kbLastKeyTimer = setTimeout(() => { _kbLastKey = null; }, 800);
  }

  // Table row keyboard nav
  const focused = document.activeElement;
  if (focused?.matches('tr[tabindex]')) {
    const rows = [...focused.closest('tbody').querySelectorAll('tr[tabindex]')];
    const idx  = rows.indexOf(focused);
    if (e.key === 'ArrowDown')  { e.preventDefault(); rows[Math.min(idx + 1, rows.length - 1)]?.focus(); return; }
    if (e.key === 'ArrowUp')    { e.preventDefault(); rows[Math.max(idx - 1, 0)]?.focus(); return; }
    if (e.key === 'Home')       { e.preventDefault(); rows[0]?.focus(); return; }
    if (e.key === 'End')        { e.preventDefault(); rows[rows.length - 1]?.focus(); return; }
    if (e.key === 'Enter') {
      const href = focused.dataset.href ?? (focused.dataset.model ? `/model/${encodeURIComponent(focused.dataset.model)}` : null);
      if (href) location.href = href;
    }
  }
});

// ---- pill / range selector helper ----
const _PILL_SEL = '[data-range],[data-metric],[data-window],[data-scale]';

function _wireRange(groupId, onChange) {
  const group = $(groupId);
  if (!group) return;
  group.querySelectorAll(_PILL_SEL).forEach(btn => {
    btn.addEventListener('click', () => {
      group.querySelectorAll(_PILL_SEL).forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      onChange(btn.dataset.range ?? btn.dataset.metric ?? btn.dataset.window ?? btn.dataset.scale);
    });
  });
}

// ---- models panel ----
let _mRange    = 'today';
let _mFetching = false;

const _FAMILY_COLOR = { opus: 'var(--cool)', sonnet: 'var(--amber)', haiku: 'var(--green)' };
const _familyColor  = n => _FAMILY_COLOR[Object.keys(_FAMILY_COLOR).find(k => n.includes(k))] ?? 'var(--mute)';
const _metricCls    = (field, v) => { const c = _TILE_CFG[field]; return c ? c.colour(v, _tileGoals[field] ?? {}) : ''; };

function _shareBarTd(share) {
  if (share == null) return '<td>–</td>';
  const pct = Math.round(share * 100);
  return `<td><div class="inline-bar-wrap"><div class="inline-bar-track"><div class="inline-bar-fill bar-good" style="width:${pct}%"></div></div><span>${pct}%</span></div></td>`;
}

function _paintModels(data) {
  const tbody = $('models-tbody');
  const emptyEl = $('models-empty');
  const sumEl   = $('models-summary');
  if (!tbody) return;
  const { rows = [], aggregate = {} } = data;
  if (sumEl) {
    const parts = [];
    if (aggregate.active_count != null) parts.push(`<span class="k">models</span> <span class="v">${aggregate.active_count}</span>`);
    if (aggregate.dominant) {
      const pct = aggregate.dominant_share != null ? ` ${Math.round(aggregate.dominant_share * 100)}%` : '';
      parts.push(`<span class="k">dominant</span> <span class="v">${esc(aggregate.dominant)}${pct}</span>`);
    }
    if (aggregate.cost_split) {
      const segs = Object.entries(aggregate.cost_split).map(([n, s]) =>
        `<span style="display:inline-block;width:${(s*100).toFixed(1)}%;height:8px;background:${_familyColor(n)};border-radius:1px" title="${esc(n)}: ${Math.round(s*100)}%"></span>`
      ).join('');
      if (segs) parts.push(`<span class="k">split</span> <span class="cost-split-bar">${segs}</span>`);
    }
    sumEl.innerHTML = parts.join('<span class="sep">·</span>');
  }
  if (!rows.length) {
    tbody.innerHTML = '';
    emptyEl?.classList.remove('hidden');
    return;
  }
  emptyEl?.classList.add('hidden');
  tbody.innerHTML = rows.map(r => {
    const win      = r.context_window_k != null ? `<span class="mute"> ${r.context_window_k}k</span>` : '';
    const toolTd   = r.tool_error_rate
      ? `<td class="num ${r.tool_error_rate >= 0.25 ? 'alarm' : 'amber'}">${(r.tool_error_rate*100).toFixed(1)}%</td>`
      : '<td class="mute">—</td>';
    return `<tr data-model="${esc(r.model)}" tabindex="0">
      <td>${esc(r.short_name)}${win}</td>
      <td class="num">${r.turns}</td>
      <td class="num">${fmtTok(r.output_tokens)}</td>
      <td class="num">${fmtUSD(r.cost_usd)}</td>
      ${_shareBarTd(r.cost_share)}
      <td class="num ${_metricCls('cost_per_kw', r.cost_per_kw)}">${r.cost_per_kw != null ? fmtUSD(r.cost_per_kw) : '–'}</td>
      <td class="num ${_metricCls('ctx_ratio',   r.ctx_ratio)}">${r.ctx_ratio   != null ? fmtRatio(r.ctx_ratio) : '–'}</td>
      <td class="num ${_metricCls('cache_reuse', r.cache_reuse)}">${r.cache_reuse != null ? fmtRatio(r.cache_reuse) : '–'}</td>
      <td class="num ${_metricCls('hit_pct',     r.hit_pct)}">${r.hit_pct != null ? r.hit_pct.toFixed(1) + '%' : '–'}</td>
      ${toolTd}
    </tr>`;
  }).join('');
  tbody.querySelectorAll('tr[data-model]').forEach(row =>
    row.addEventListener('click', () => { location.href = `/model/${encodeURIComponent(row.dataset.model)}`; })
  );
}

function _renderModels(payload) {
  const models = payload.models;
  if (!models || _mRange !== 'today') return;
  _paintModels(models);
}

async function _fetchModels() {
  if (_mFetching) return;
  _mFetching = true;
  try {
    const res = await fetch(`/api/models?range=${_mRange}`);
    if (res.ok) _paintModels(await res.json());
  } finally { _mFetching = false; }
}

function _wireModels() {
  _wireRange('models-range-pills', r => { _mRange = r; _fetchModels(); });
}

// ---- recent activity panel ----
let _raWindow   = '60m';
let _raFetching = false;
let _raData     = null;
let _raSortCol  = null;
let _raSortDir  = -1;  // -1 = desc (most recent first by default)

function _paintRecent(data) {
  _raData = data;
  _drawRecentTable();
}

function _drawRecentTable() {
  if (!_raData) return;
  const { rows = [], aggregate = {} } = _raData;
  const sumEl   = $('recent-summary');
  const tbody   = $('recent-tbody');
  const emptyEl = $('recent-empty');
  if (!tbody) return;

  if (sumEl) {
    const parts = [];
    if (aggregate.projects   != null) parts.push(`<span class="k">projects</span> <span class="v">${aggregate.projects}</span>`);
    if (aggregate.turns      != null) parts.push(`<span class="k">turns</span> <span class="v">${aggregate.turns}</span>`);
    if (aggregate.cost       != null) parts.push(`<span class="k">cost</span> <span class="v">${fmtUSD(aggregate.cost)}</span>`);
    if (aggregate.cost_per_kw != null) parts.push(`<span class="k">$/kW</span> <span class="v ${_metricCls('cost_per_kw', aggregate.cost_per_kw)}">${fmtUSD(aggregate.cost_per_kw)}</span>`);
    if (aggregate.hit_pct    != null) parts.push(`<span class="k">hit%</span> <span class="v ${_metricCls('hit_pct', aggregate.hit_pct)}">${aggregate.hit_pct.toFixed(1)}%</span>`);
    if (aggregate.output     != null) parts.push(`<span class="k">output</span> <span class="v">${fmtTok(aggregate.output)}</span>`);
    if (aggregate.model_mix) {
      const top = Object.entries(aggregate.model_mix).sort((a, b) => b[1] - a[1])[0];
      if (top) parts.push(`<span class="k">model</span> <span class="v">${esc(top[0])} ${Math.round(top[1] * 100)}%</span>`);
    }
    sumEl.innerHTML = parts.join('<span class="sep">·</span>');
  }

  // Sort
  const sorted = [...rows];
  if (_raSortCol) {
    sorted.sort((a, b) => {
      const va = a[_raSortCol] ?? (_raSortDir > 0 ? -Infinity : Infinity);
      const vb = b[_raSortCol] ?? (_raSortDir > 0 ? -Infinity : Infinity);
      return va < vb ? -_raSortDir : va > vb ? _raSortDir : 0;
    });
  }

  // Update sort indicators
  const tbl = $('recent-tbl');
  tbl?.querySelectorAll('th[data-sort]').forEach(th => {
    th.classList.toggle('sort-asc',  th.dataset.sort === _raSortCol && _raSortDir > 0);
    th.classList.toggle('sort-desc', th.dataset.sort === _raSortCol && _raSortDir < 0);
  });

  if (!sorted.length) {
    tbody.innerHTML = '';
    emptyEl?.classList.remove('hidden');
    return;
  }
  emptyEl?.classList.add('hidden');

  tbody.innerHTML = sorted.map(r => {
    const ctxPct = r.ctx_used != null ? Math.round(r.ctx_used * 100) : null;
    const ctxCls = ctxPct != null ? (ctxPct >= 85 ? 'alarm' : ctxPct >= 70 ? 'amber' : '') : '';
    const sessLink = r.latest_session_id
      ? `<td><a href="/session/${r.latest_session_id}" class="ext-link" title="Latest session" tabindex="-1">↗</a></td>`
      : '<td></td>';
    return `<tr data-href="/project/${r.cwd_b64}" tabindex="0" title="${esc(r.cwd ?? '')}">
      <td>${cwdBasename(r.cwd)}</td>
      <td>${esc(r.model_primary)}</td>
      <td>${fmtRelTime(r.last_turn_at)}</td>
      <td class="num">${r.turns}</td>
      <td class="num">${fmtTok(r.output)}</td>
      <td class="num ${ctxCls}">${ctxPct != null ? ctxPct + '%' : '–'}</td>
      <td class="num ${_metricCls('cost_per_kw', r.cost_per_kw)}">${r.cost_per_kw != null ? fmtUSD(r.cost_per_kw) : '–'}</td>
      <td class="num ${_metricCls('ctx_ratio',   r.ctx_ratio)}">${r.ctx_ratio   != null ? fmtRatio(r.ctx_ratio) : '–'}</td>
      <td class="num ${_metricCls('cache_reuse', r.cache_reuse)}">${r.cache_reuse != null ? fmtRatio(r.cache_reuse) : '–'}</td>
      <td class="num ${_metricCls('hit_pct',     r.hit_pct)}">${r.hit_pct != null ? r.hit_pct.toFixed(1) + '%' : '–'}</td>
      <td>${verdictPill(r.verdict)}</td>
      ${sessLink}
    </tr>`;
  }).join('');

  tbody.querySelectorAll('tr[data-href]').forEach(row =>
    row.addEventListener('click', e => {
      if (e.target.closest('a')) return;
      location.href = row.dataset.href;
    })
  );
}

function _renderRecent(payload) {
  const ra = payload.recent_activity;
  if (!ra || _raWindow !== '60m') return;
  _paintRecent(ra);
}

async function _fetchRecent() {
  if (_raFetching) return;
  _raFetching = true;
  try {
    const res = await fetch(`/api/recent?window=${_raWindow}`);
    if (res.ok) _paintRecent(await res.json());
  } finally { _raFetching = false; }
}

function _wireRecent() {
  _wireRange('recent-window-pills', w => { _raWindow = w; _fetchRecent(); });

  $('recent-expand-link')?.addEventListener('click', () => {
    _raWindow = '24h';
    const group = $('recent-window-pills');
    group?.querySelectorAll(_PILL_SEL).forEach(b => b.classList.toggle('on', b.dataset.window === '24h'));
    _fetchRecent();
  });

  $('recent-tbl')?.querySelectorAll('th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.sort;
      _raSortDir = _raSortCol === col ? -_raSortDir : -1;
      _raSortCol = col;
      _drawRecentTable();
    });
  });
}

// ---- settings ----
const _THRESH_INPUTS = {
  hit_rate_good_pct: 'pref-hit-good',
  hit_rate_red_pct:  'pref-hit-red',
  cost_per_kw_good:  'pref-cost-good',
  cost_per_kw_red:   'pref-cost-red',
  ctx_ratio_red:     'pref-ctx-red',
  cache_reuse_good:  'pref-cache-good',
  cache_reuse_red:   'pref-cache-red',
};

const _ASSUMPTION_LABELS = {
  window_boundary_heuristic: 'Window boundary heuristic',
  unknown_model_fallback:    'Unknown model fallback',
  dedup_passthrough:         'Dedup passthrough',
  interrupted_turn_skipped:  'Interrupted turn skipped',
  gemini_unpriced:           'Gemini unpriced',
};

function _loadSettings(payload) {
  if (payload.config?.tick_seconds) {
    const ticks = $('pref-tick-pills');
    ticks?.querySelectorAll('[data-tick]').forEach(btn =>
      btn.classList.toggle('on', +btn.dataset.tick === payload.config.tick_seconds)
    );
  }
  if (payload.thresholds) {
    for (const [key, id] of Object.entries(_THRESH_INPUTS)) {
      const el = $(id);
      if (el) el.value = payload.thresholds[key] ?? '';
    }
  }
  if (payload.assumptions_summary) {
    const el = $('assumptions-summary');
    if (el) {
      const rows = Object.entries(payload.assumptions_summary)
        .filter(([, v]) => v > 0)
        .map(([k, v]) => `<div class="assumption-row"><span>${_ASSUMPTION_LABELS[k] ?? k}</span><span class="num">${v}</span></div>`)
        .join('');
      el.innerHTML = rows || '<div class="mute">None fired this session.</div>';
    }
  }
}

async function _postPrefs(body) {
  const res = await fetch('/api/prefs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res.ok ? res.json() : null;
}

function _wireSettings() {
  $('pref-tick-pills')?.querySelectorAll('[data-tick]').forEach(btn => {
    btn.addEventListener('click', async () => {
      $('pref-tick-pills').querySelectorAll('[data-tick]').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      await _postPrefs({ tick_seconds: +btn.dataset.tick });
    });
  });

  $('pref-save-btn')?.addEventListener('click', async () => {
    const thresholds = {};
    for (const [key, id] of Object.entries(_THRESH_INPUTS)) {
      const el = $(id);
      if (el) thresholds[key] = +el.value;
    }
    const prefs = await _postPrefs({ thresholds });
    if (prefs?.thresholds) {
      for (const [key, id] of Object.entries(_THRESH_INPUTS)) {
        const el = $(id);
        if (el) el.value = prefs.thresholds[key] ?? '';
      }
    }
  });

  $('pref-reset-btn')?.addEventListener('click', async () => {
    const prefs = await _postPrefs({ thresholds: 'reset' });
    if (prefs?.thresholds) {
      for (const [key, id] of Object.entries(_THRESH_INPUTS)) {
        const el = $(id);
        if (el) el.value = prefs.thresholds[key] ?? '';
      }
    }
  });
}

// ---- find / search ----
let _searchTimer = null;

function _wireFind() {
  const input   = $('search-input');
  const results = $('search-results');
  if (!input || !results) return;

  $('m-search')?.addEventListener('change', e => { if (e.target.checked) input.focus(); });

  const _getFocused = () => results.querySelector('.search-result-item.focused');
  const _getItems   = () => [...results.querySelectorAll('.search-result-item')];

  input.addEventListener('input', () => {
    clearTimeout(_searchTimer);
    const q = input.value.trim();
    if (!q) { results.innerHTML = ''; return; }
    _searchTimer = setTimeout(async () => {
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      if (!res.ok) return;
      const { hits } = await res.json();
      results.innerHTML = hits.length
        ? hits.map((h, i) => `<div class="search-result-item${i === 0 ? ' focused' : ''}" data-href="${esc(h.href)}">
            <span class="search-result-kind">${esc(h.kind)}</span>
            <span class="search-result-label">${esc(h.label)}</span>
          </div>`).join('')
        : '<div class="mute" style="padding:8px">No results</div>';
      results.querySelectorAll('.search-result-item').forEach(el =>
        el.addEventListener('click', () => { location.href = el.dataset.href; })
      );
    }, 250);
  });

  input.addEventListener('keydown', e => {
    const items = _getItems();
    if (!items.length) return;
    const focused = _getFocused();
    const idx = focused ? items.indexOf(focused) : -1;
    if (e.key === 'ArrowDown')  { e.preventDefault(); focused?.classList.remove('focused'); items[Math.min(idx + 1, items.length - 1)]?.classList.add('focused'); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); focused?.classList.remove('focused'); items[Math.max(idx - 1, 0)]?.classList.add('focused'); }
    else if (e.key === 'Enter' && focused) { location.href = focused.dataset.href; }
  });
}

// ---- boot ----
_initGlossary();
_initTzLabel();
_wireModalBackdrops();
_wirePeriodPills();
_wireHourly();
_wireDaily();
_wireModels();
_wireRecent();
_wireSettings();
_wireFind();
const _p0 = _getPeriod();
_fetchSnapshot(_p0);
_connect(_p0);
