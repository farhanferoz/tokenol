const overlay = document.getElementById('settings-overlay');
const btn     = document.getElementById('settings-btn');
const backdrop = document.getElementById('settings-backdrop');

function openSettings()  { overlay.classList.add('open'); }
function closeSettings() { overlay.classList.remove('open'); }

btn?.addEventListener('click', openSettings);
backdrop?.addEventListener('click', closeSettings);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSettings();
  if (e.key === 's' && !e.target.matches('input,textarea')) openSettings();
});
