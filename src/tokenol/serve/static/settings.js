const STORAGE_KEY = 'tokenol:prefs:v1';

const DEFAULTS = {
  burnLookback:  '5m',
  sessionsRange: '24h',
  projectsRange: '24h',
  modelsRange:   '24h',
  dailyRange:    14,
  hideSidechain: false,
  referenceUsd:  50,
  tickSeconds:   5,
  idleBackoff:   true,
  reduceMotion:  'auto',
};

function loadPrefs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch (_) {}
  return { ...DEFAULTS };
}

window.savePrefs = () => {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      burnLookback:  window.PREFS.burnLookback,
      sessionsRange: window.PREFS.sessionsRange,
      projectsRange: window.PREFS.projectsRange,
      modelsRange:   window.PREFS.modelsRange,
      dailyRange:    window.PREFS.dailyRange,
      hideSidechain: window.PREFS.hideSidechain,
      referenceUsd:  window.PREFS.referenceUsd  ?? DEFAULTS.referenceUsd,
      tickSeconds:   window.PREFS.tickSeconds   ?? DEFAULTS.tickSeconds,
      idleBackoff:   window.PREFS.idleBackoff   ?? DEFAULTS.idleBackoff,
      reduceMotion:  window.PREFS.reduceMotion  ?? DEFAULTS.reduceMotion,
    }));
  } catch (_) {}
};

function postServerPrefs(changes) {
  fetch('/api/prefs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(changes),
  }).catch(() => {});
}

function spmKey(r) { return r === '1' ? '24h' : r === '7' ? '7d' : '14d'; }
function spmVal(range) { return range === '24h' ? '1' : range === '7d' ? '7' : '14'; }

function setActive(groupId, val) {
  document.getElementById(groupId)?.querySelectorAll('button').forEach(btn => {
    const bVal = btn.dataset.lb ?? btn.dataset.r ?? btn.dataset.v;
    btn.classList.toggle('active', bVal === String(val));
  });
}

function applyReduceMotion(val) {
  document.documentElement.classList.toggle('reduce-motion', val === 'on');
}

// Re-sync settings panel buttons from current window.PREFS. Called on open.
function syncSettingsButtons() {
  const p = window.PREFS;
  setActive('lb-opts',          p.burnLookback);
  setActive('spm-range-opts',   spmVal(p.sessionsRange));
  setActive('daily-range-opts', String(p.dailyRange));
  setActive('ref-opts',         String(p.referenceUsd ?? DEFAULTS.referenceUsd));
  setActive('tick-opts',        String(p.tickSeconds  ?? DEFAULTS.tickSeconds));
  setActive('idle-opts',        (p.idleBackoff ?? true) ? 'on' : 'off');
  setActive('sidechain-opts',   p.hideSidechain ? 'on' : 'off');
  setActive('motion-opts',      p.reduceMotion  ?? DEFAULTS.reduceMotion);
}

function applyLoadedPrefs(p) {
  Object.assign(window.PREFS, {
    burnLookback:  p.burnLookback,
    sessionsRange: p.sessionsRange,
    projectsRange: p.projectsRange,
    modelsRange:   p.modelsRange,
    dailyRange:    +p.dailyRange,
    hideSidechain: !!p.hideSidechain,
    referenceUsd:  +p.referenceUsd,
    tickSeconds:   +p.tickSeconds,
    idleBackoff:   !!p.idleBackoff,
    reduceMotion:  p.reduceMotion,
  });

  syncSettingsButtons();

  setActive('burn-lookback',      p.burnLookback);
  setActive('sessions-range-sel', spmVal(p.sessionsRange));
  setActive('models-range-sel',   spmVal(p.modelsRange));
  setActive('projects-range-sel', spmVal(p.projectsRange));
  setActive('daily-range-sel',    String(p.dailyRange));

  applyReduceMotion(p.reduceMotion);

  // Re-render all panels (no-op if SSE data not yet received)
  window.rerender?.(['active_window', 'config', 'today', 'daily_90d',
                     'sessions', 'models', 'projects', 'heatmap_14d', 'recent_turns']);

  // Restore server-side prefs if they differ from server defaults
  const changes = {};
  if (+p.referenceUsd !== DEFAULTS.referenceUsd) changes.reference_usd = +p.referenceUsd;
  if (+p.tickSeconds  !== DEFAULTS.tickSeconds)  changes.tick_seconds  = +p.tickSeconds;
  if (Object.keys(changes).length) postServerPrefs(changes);
}

// ---- apply on load ----

applyLoadedPrefs(loadPrefs());

// ---- wire settings panel ----

function wireOpt(groupId, onChange) {
  const group = document.getElementById(groupId);
  group?.querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => {
      group.querySelectorAll('button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      onChange(btn.dataset.v);
      window.savePrefs();
    });
  });
}

wireOpt('lb-opts', val => {
  window.PREFS.burnLookback = val;
  setActive('burn-lookback', val);
  window.rerender?.(['active_window']);
});

wireOpt('spm-range-opts', val => {
  const range = spmKey(val);
  window.PREFS.sessionsRange = range;
  window.PREFS.projectsRange = range;
  window.PREFS.modelsRange   = range;
  setActive('sessions-range-sel', val);
  setActive('models-range-sel',   val);
  setActive('projects-range-sel', val);
  window.rerender?.(['sessions', 'models', 'projects']);
});

wireOpt('daily-range-opts', val => {
  window.PREFS.dailyRange = parseInt(val, 10);
  setActive('daily-range-sel', val);
  window.rerender?.(['daily_90d']);
});

wireOpt('ref-opts', val => {
  window.PREFS.referenceUsd = parseFloat(val);
  postServerPrefs({ reference_usd: parseFloat(val) });
});

wireOpt('tick-opts', val => {
  window.PREFS.tickSeconds = parseInt(val, 10);
  postServerPrefs({ tick_seconds: parseInt(val, 10) });
});

wireOpt('idle-opts', val => {
  window.PREFS.idleBackoff = (val === 'on');
});

wireOpt('sidechain-opts', val => {
  window.PREFS.hideSidechain = (val === 'on');
  window.rerender?.(['recent_turns']);
});

wireOpt('motion-opts', val => {
  window.PREFS.reduceMotion = val;
  applyReduceMotion(val);
});

// ---- reset ----

document.getElementById('settings-reset')?.addEventListener('click', () => {
  localStorage.removeItem(STORAGE_KEY);
  applyLoadedPrefs({ ...DEFAULTS });
  postServerPrefs({ reference_usd: DEFAULTS.referenceUsd, tick_seconds: DEFAULTS.tickSeconds });
});

// ---- open / close ----

const overlay  = document.getElementById('settings-overlay');
const settBtn  = document.getElementById('settings-btn');
const backdrop = document.getElementById('settings-backdrop');

function openSettings()  { syncSettingsButtons(); overlay.classList.add('open'); }
function closeSettings() { overlay.classList.remove('open'); }

settBtn?.addEventListener('click', openSettings);
backdrop?.addEventListener('click', closeSettings);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSettings();
  if ((e.key === 's' || e.key === '?') && !e.target.matches('input,textarea')) openSettings();
});
