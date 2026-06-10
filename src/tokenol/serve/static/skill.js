import { renderRankedBars, fmtUSD, fmtTok, esc } from '/assets/components.js';

const $ = (id) => document.getElementById(id);
const name = decodeURIComponent(location.pathname.split('/').pop() || '');

function renderScorecards(sc) {
  const cards = [
    { label: 'Est. Cost', primary: fmtUSD(sc.cost_usd),
      sub: `~${(sc.share_of_total * 100).toFixed(1)}% of total spend` },
    { label: 'Output tokens', primary: fmtTok(sc.output_tokens),
      sub: sc.invocations ? `avg ${fmtTok(sc.output_tokens / sc.invocations)} / use` : '' },
    { label: 'Invocations', primary: sc.invocations.toLocaleString('en-US'),
      sub: sc.invocations_7d != null ? `7-day: ${sc.invocations_7d.toLocaleString('en-US')}` : '' },
    { label: 'Top project', primary: sc.top_project.name || '—',
      sub: sc.top_project.cost_usd > 0
        ? `${fmtUSD(sc.top_project.cost_usd)} (${(sc.top_project.share * 100).toFixed(0)}%)` : '' },
  ];
  $('skill-scorecards').innerHTML = cards.map((c) => `
    <article class="scorecard-card">
      <div class="sc-label">${esc(c.label)}</div>
      <div class="sc-primary">${esc(c.primary)}</div>
      <div class="sc-sub">${esc(c.sub)}</div>
    </article>`).join('');
}

function renderSplit(split) {
  const inline = split.inline_usd || 0, sub = split.subagent_usd || 0;
  $('skill-split').textContent = `inline ${fmtUSD(inline)} · sub-agents ${fmtUSD(sub)}`;
}

function renderDailyChart(daily, totalCost) {
  $('skill-daily-total').textContent = 'total ' + fmtUSD(totalCost);
  let peak = { date: null, cost: 0 };
  for (const d of daily) if (d.cost_usd > peak.cost) peak = { date: d.date, cost: d.cost_usd };
  $('skill-daily-peak').textContent = peak.date ? `peak ${peak.date.slice(5)} · ${fmtUSD(peak.cost)}` : '';
  if (typeof window.Chart === 'undefined') return;
  new window.Chart($('chart-skill-daily'), {
    type: 'line',
    data: { labels: daily.map((p) => p.date.slice(5)),
      datasets: [{ data: daily.map((p) => p.cost_usd), borderColor: '#a66408',
        backgroundColor: 'rgba(166, 100, 8, 0.18)', fill: true, tension: 0.2, pointRadius: 0 }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, ticks: { callback: fmtUSD } },
                x: { ticks: { maxTicksLimit: 6 } } } },
  });
}

async function load() {
  $('skill-name').textContent = name;
  document.title = `tokenol — ${name}`;
  try {
    const resp = await fetch('/api/skill/' + encodeURIComponent(name));
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    renderScorecards(data.scorecards);
    renderSplit(data.split);
    const total = data.daily_cost.reduce((s, d) => s + (d.cost_usd || 0), 0);
    renderDailyChart(data.daily_cost, total);
    if (!data.by_model.length) {
      $('skill-no-models').classList.remove('hidden');
    } else {
      renderRankedBars($('skill-by-model'),
        data.by_model.map((r) => ({ label: r.name, value: r.cost_usd,
          href: '/model/' + encodeURIComponent(r.name) })),
        { valueFormat: fmtUSD });
    }
    if (!data.by_project.length) {
      $('skill-no-projects').classList.remove('hidden');
    } else {
      renderRankedBars($('skill-by-project'),
        data.by_project.map((r) => ({ label: r.project_label || r.name,
          sublabel: r.last_active ? r.last_active.slice(0, 10) : '',
          value: r.cost_usd, href: r.cwd_b64 ? '/project/' + r.cwd_b64 : undefined })),
        { valueFormat: fmtUSD });
    }
  } catch (err) {
    const el = $('skill-error');
    if (el) { el.textContent = 'Failed to load: ' + err.message; el.classList.remove('hidden'); }
  }
}

load();
