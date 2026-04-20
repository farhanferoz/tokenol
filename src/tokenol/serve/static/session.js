// ---- helpers ----

const $ = id => document.getElementById(id);

const CV = {};
(function () {
  const s = getComputedStyle(document.documentElement);
  ['--amber', '--cool', '--alarm', '--mute', '--rule', '--amber-dim'].forEach(n => {
    CV[n] = s.getPropertyValue(n).trim();
  });
})();

const AMBER_RGB     = '255,182,71';
const AMBER_DIM_RGB = '138,103,48';
const COOL_RGB      = '111,174,216';

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

  renderChart(d.turns);
  renderTimeline(d.turns, d.first_ts, d.last_ts);
  initTable(d.turns);
}

// ---- turn chart (uPlot) ----

let _uplot = null;

function renderChart(turns) {
  if (!turns.length) { $('session-chart-section').style.display = 'none'; return; }

  const cont = $('turn-chart');
  const xs   = turns.map(t => new Date(t.ts).getTime() / 1000);
  const inp  = turns.map(t => t.input_tokens + t.cache_creation_tokens);
  const crd  = turns.map(t => t.cache_read_tokens);
  const out  = turns.map(t => t.output_tokens);

  _uplot = new uPlot({
    width:  cont.offsetWidth || 800,
    height: 260,
    cursor: { show: true, points: { size: 6, fill: CV['--bg'] } },
    legend: { show: false },
    padding: [4, 4, 0, 0],
    select: { show: false },
    axes: [
      { stroke: CV['--mute'], ticks: { show: false }, grid: { show: false }, size: 28 },
      { stroke: CV['--mute'], ticks: { show: false }, grid: { stroke: CV['--rule'], width: 1 }, size: 50 },
    ],
    series: [
      {},
      { stroke: CV['--amber'],     width: 1.5, fill: `rgba(${AMBER_RGB},0.15)`,     label: 'input+creation' },
      { stroke: CV['--amber-dim'], width: 1,   fill: `rgba(${AMBER_DIM_RGB},0.10)`, label: 'cache read'     },
      { stroke: CV['--cool'],      width: 1.5, fill: `rgba(${COOL_RGB},0.12)`,      label: 'output'         },
    ],
    scales: { y: { range: (_, _min, max) => [0, Math.max(max * 1.1, 100)] } },
  }, [xs, inp, crd, out], cont);

  _uplot.over.style.cursor = 'crosshair';
  _uplot.over.addEventListener('click', () => {
    const idx = _uplot.cursor.idx;
    if (idx != null) highlightTurn(idx);
  });
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
let _turns   = [];
let _sortKey = 'ts';
let _sortAsc = true;
let _page    = 0;

function initTable(turns) {
  _turns = turns;

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
    const origIdx = _turns.indexOf(t);
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
  const sortedPos = sorted.findIndex(t => _turns.indexOf(t) === origIdx);
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
