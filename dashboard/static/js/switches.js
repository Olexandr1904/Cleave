// switches.js — running/paused switch panel (global pill + one pill per repo)

import { loadSwitches, setSwitch } from './api.js';
import { esc } from './helpers.js';

const ICON = { running: '▶', paused: '⏸' };
const OPPOSITE = { running: 'paused', paused: 'running' };

function paint(container, mode, snap) {
  const data = snap[mode] || { global: 'paused', repos: [] };
  const label = mode.charAt(0).toUpperCase() + mode.slice(1);
  const repoPills = data.repos.map(r => {
    const inherited = r.state === 'running' && r.effective === 'paused';
    return `<span class="switch-pill ${esc(r.effective)}${inherited ? ' inherited' : ''}"
        data-scope="${esc(r.scope)}" data-state="${esc(r.state)}"
        ${inherited ? `title="Paused globally — start ${esc(label)} first"` : ''}>${esc(r.scope)} ${ICON[r.effective]}</span>`;
  }).join('');
  container.innerHTML = `<div class="switch-panel">
    <span class="switch-pill ${esc(data.global)}" data-scope="*" data-state="${esc(data.global)}">${esc(label)}: ${esc(data.global)} ${ICON[data.global]}</span>
    ${repoPills}
  </div>`;
  // An inherited pill (paused only by the global switch) takes no clicks: a PUT would
  // silently pause the repo's own switch.
  container.querySelectorAll('.switch-pill:not(.inherited)').forEach(pill => {
    pill.addEventListener('click', async () => {
      try {
        const next = await setSwitch(mode, OPPOSITE[pill.dataset.state], pill.dataset.scope);
        paint(container, mode, next);
      } catch (err) {
        alert('Switch failed: ' + err.message);
      }
    });
  });
}

export async function renderSwitchPanel(container, mode) {
  if (!container) return;
  try {
    paint(container, mode, await loadSwitches());
  } catch (e) {
    container.innerHTML = '';
  }
}
