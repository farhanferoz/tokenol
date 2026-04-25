import { fmtRelTime, cwdBasename, esc } from '/assets/components.js';

const $ = id => document.getElementById(id);
const name = decodeURIComponent(location.pathname.split('/').pop());

fetch(`/api/tool/${encodeURIComponent(name)}`)
  .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
  .then(render)
  .catch(err => {
    const el = $('tool-error');
    if (el) { el.classList.remove('hidden'); el.textContent = `Failed to load: ${esc(String(err))}`; }
  });

function render(d) {
  document.title = `tokenol — ${d.name}`;
  $('tool-name').textContent = d.name;

  const sumEl = $('tool-summary');
  if (sumEl) {
    sumEl.innerHTML = [
      `<span class="k">invocations</span> <span class="v">${d.total_invocations.toLocaleString('en-US')}</span>`,
      `<span class="k">projects</span> <span class="v">${d.projects_using_tool.length}</span>`,
      `<span class="k">models</span> <span class="v">${d.models_using_tool.length}</span>`,
    ].join('<span class="sep">·</span>');
  }

  const projTbody = $('tool-projects-tbody');
  const projEmpty = $('tool-no-projects');
  const projects = d.projects_using_tool ?? [];
  if (!projects.length) {
    projEmpty?.classList.remove('hidden');
  } else {
    projEmpty?.classList.add('hidden');
    projTbody.innerHTML = projects.map(p =>
      `<tr ${p.cwd_b64 ? `style="cursor:pointer" data-href="/project/${esc(p.cwd_b64)}"` : ''}>
        <td title="${esc(p.cwd ?? '')}">${cwdBasename(p.cwd)}</td>
        <td class="num">${p.count.toLocaleString('en-US')}</td>
        <td>${p.last_active ? fmtRelTime(p.last_active) : '–'}</td>
      </tr>`
    ).join('');
    projTbody.querySelectorAll('tr[data-href]').forEach(row =>
      row.addEventListener('click', () => { location.href = row.dataset.href; })
    );
  }

  const modelTbody = $('tool-models-tbody');
  const modelEmpty = $('tool-no-models');
  const models = d.models_using_tool ?? [];
  if (!models.length) {
    modelEmpty?.classList.remove('hidden');
  } else {
    modelEmpty?.classList.add('hidden');
    modelTbody.innerHTML = models.map(m =>
      `<tr style="cursor:pointer" data-href="/model/${encodeURIComponent(m.model)}">
        <td>${esc(m.model)}</td>
        <td class="num">${m.count.toLocaleString('en-US')}</td>
      </tr>`
    ).join('');
    modelTbody.querySelectorAll('tr[data-href]').forEach(row =>
      row.addEventListener('click', () => { location.href = row.dataset.href; })
    );
  }
}
