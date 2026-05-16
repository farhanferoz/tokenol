import {
  fmtUSD, fmtPct, shortModel, esc,
  verdictPill, renderVerdictDist, deltaBadge, cwdBasename,
} from '/assets/components.js';

const $ = id => document.getElementById(id);

const targetDate = location.pathname.split('/').pop();

fetch(`/api/day/${targetDate}`)
  .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
  .then(render)
  .catch(err => {
    const el = $('day-error');
    el.style.display = '';
    el.textContent = `Failed to load day: ${esc(String(err.message))}`;
  });

function render(d) {
  document.title = `tokenol — ${d.date}`;
  $('day-date').textContent      = d.date;
  $('day-date-full').textContent = d.date;
  $('day-cost').textContent      = fmtUSD(d.total_cost);

  const deltaEl = $('day-delta');
  if (deltaEl) {
    if (d.delta_vs_7d_median != null) {
      const pct = Math.round((d.delta_vs_7d_median - 1) * 100);
      const cls = d.delta_vs_7d_median >= 1.5 ? 'alarm' : d.delta_vs_7d_median >= 1.2 ? 'amber' : 'mute';
      deltaEl.textContent = pct > 0 ? `↑${pct}%` : pct < 0 ? `↓${Math.abs(pct)}%` : 'normal';
      deltaEl.className   = `val ${cls}`;
    } else {
      deltaEl.textContent = '–';
    }
  }

  if (d.anomaly_flags?.length) {
    const b = $('day-anomaly-badge');
    if (b) b.style.display = '';
  }

  renderHourly(d.hourly);
  renderSessions(d.top_sessions);
  renderProjects(d.top_projects);
  renderVerdictDist('day-verdict-dist', d.verdict_distribution);
  highlightTargetHour();
}

function highlightTargetHour() {
  const m = location.hash.match(/^#hour=(\d+)$/);
  if (!m) return;
  const bar = document.querySelector(`.day-hour-bar[data-hour="${+m[1]}"]`);
  if (!bar) return;
  bar.classList.add('target');
  bar.scrollIntoView({ block: 'nearest', inline: 'center' });
}

function renderHourly(hourly) {
  const container = $('hourly-bar-chart');
  if (!container || !hourly) return;
  const maxCost = Math.max(...hourly.map(h => h.cost_usd), 0.0001);
  container.innerHTML = hourly.map(h => {
    const pct = Math.max(h.cost_usd / maxCost * 100, h.cost_usd > 0 ? 4 : 2);
    const topModels = Object.entries(h.model_mix ?? {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 2)
      .map(([m, n]) => `${m.replace(/^claude-/,'').slice(0,10)}: ${n}`)
      .join(', ');
    const isAnomalous = h.cost_usd > 0 && false; // flag via backend if needed
    return `<div class="day-hour-bar${isAnomalous ? ' anomaly' : ''}"
      data-hour="${h.hour}"
      style="height:${pct}%"
      title="${String(h.hour).padStart(2,'0')}:00 — ${fmtUSD(h.cost_usd)} · ${h.turns} turns${topModels ? '\n' + topModels : ''}"></div>`;
  }).join('');
}

function renderSessions(sessions) {
  const tbody = $('day-sessions-tbody');
  if (!tbody) return;
  tbody.innerHTML = (sessions || []).slice(0, 15).map(s => `<tr style="cursor:pointer" data-id="${esc(s.id)}">
    <td>${esc(s.id.slice(0,8))}</td>
    <td>${shortModel(s.model)}</td>
    <td>${fmtUSD(s.cost_usd)}</td>
    <td>${s.turns}</td>
    <td>${verdictPill(s.verdict)}</td>
  </tr>`).join('');
  tbody.querySelectorAll('tr[data-id]').forEach(row => {
    row.addEventListener('click', () => { location.href = `/session/${row.dataset.id}`; });
  });
}

function renderProjects(projects) {
  const tbody = $('day-projects-tbody');
  if (!tbody) return;
  tbody.innerHTML = (projects || []).slice(0, 10).map(p => {
    const dir = cwdBasename(p.cwd);
    const hitPct = p.cache_hit_rate != null ? Math.round(p.cache_hit_rate * 100) + '%' : '–';
    return `<tr ${p.cwd_b64 ? `style="cursor:pointer" data-href="/project/${esc(p.cwd_b64)}"` : ''}>
      <td title="${esc(p.cwd ?? '')}">${esc(dir)}</td>
      <td>${fmtUSD(p.cost_usd)}</td>
      <td>${p.sessions}</td>
      <td>${hitPct}</td>
    </tr>`;
  }).join('');
  tbody.querySelectorAll('tr[data-href]').forEach(row => {
    row.addEventListener('click', () => { location.href = row.dataset.href; });
  });
}
