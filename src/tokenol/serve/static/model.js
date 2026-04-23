import { fmtUSD, fmtRelTime, cwdBasename, esc } from '/assets/components.js';

const $ = id => document.getElementById(id);
const name = decodeURIComponent(location.pathname.split('/').pop());

fetch(`/api/model/${encodeURIComponent(name)}`)
  .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
  .then(render)
  .catch(err => {
    const el = $('model-error');
    if (el) { el.classList.remove('hidden'); el.textContent = `Failed to load: ${esc(String(err))}`; }
  });

function render(d) {
  document.title = `tokenol — ${d.name}`;
  $('model-name').textContent = d.name;

  const sumEl = $('model-summary');
  if (sumEl) {
    sumEl.innerHTML = [
      `<span class="k">cost</span> <span class="v">${fmtUSD(d.total_cost)}</span>`,
      `<span class="k">turns</span> <span class="v">${d.total_turns}</span>`,
    ].join('<span class="sep">·</span>');
  }

  const barEl = $('model-cost-bar');
  if (barEl && d.cost_breakdown) {
    const b = d.cost_breakdown;
    const total = (b.input_usd + b.output_usd + b.cache_read_usd + b.cache_creation_usd) || 1;
    const segs = [
      { label: 'input',       usd: b.input_usd,          color: 'var(--mute)' },
      { label: 'output',      usd: b.output_usd,          color: 'var(--amber)' },
      { label: 'cache read',  usd: b.cache_read_usd,      color: 'var(--cool)' },
      { label: 'cache write', usd: b.cache_creation_usd,  color: 'var(--green)' },
    ].filter(s => s.usd > 0);
    barEl.innerHTML = segs.map(s =>
      `<div class="cost-bar-seg" style="flex:${(s.usd/total*100).toFixed(1)};background:${s.color}" title="${s.label}: ${fmtUSD(s.usd)}"></div>`
    ).join('');
  }

  const tbody   = $('model-projects-tbody');
  const noProj  = $('model-no-projects');
  const projects = d.projects_using_model ?? [];
  if (!projects.length) { noProj?.classList.remove('hidden'); return; }
  noProj?.classList.add('hidden');
  tbody.innerHTML = projects.map(p =>
    `<tr ${p.cwd_b64 ? `style="cursor:pointer" data-href="/project/${esc(p.cwd_b64)}"` : ''}>
      <td title="${esc(p.cwd ?? '')}">${cwdBasename(p.cwd)}</td>
      <td class="num">${fmtUSD(p.cost)}</td>
      <td class="num">${p.turns}</td>
      <td>${p.last_active ? fmtRelTime(p.last_active) : '–'}</td>
    </tr>`
  ).join('');
  tbody.querySelectorAll('tr[data-href]').forEach(row =>
    row.addEventListener('click', () => { location.href = row.dataset.href; })
  );
}
