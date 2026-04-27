// settings.js — Settings view (model picker)

import { esc } from './helpers.js';

let baseline = null;

export async function renderSettings() {
  const content = document.getElementById('content');
  content.innerHTML = '<div class="state-msg">Loading…</div>';

  try {
    const r = await fetch('/api/settings/model');
    if (!r.ok) throw new Error(`GET failed: ${r.status}`);
    const { model, options } = await r.json();
    baseline = model;

    const opts = options.map(m =>
      `<option value="${esc(m)}"${m === model ? ' selected' : ''}>${esc(m)}</option>`
    ).join('');

    content.innerHTML = `
      <section class="settings-view">
        <h2 class="settings-section-title">Claude Model</h2>
        <p class="settings-help">Used by the agent dispatcher. Changes apply to the next agent dispatch — running tickets keep their starting model for the current step.</p>
        <div class="settings-row">
          <label class="settings-label" for="settings-model">Model</label>
          <select id="settings-model" class="settings-spinner">${opts}</select>
          <button id="settings-save" class="btn-primary" disabled>Save</button>
          <span id="settings-status" class="settings-pill pill-idle"></span>
        </div>
      </section>
    `;

    const select = document.getElementById('settings-model');
    const save = document.getElementById('settings-save');
    const pill = document.getElementById('settings-status');

    select.addEventListener('change', () => {
      save.disabled = select.value === baseline;
      setPill(pill, '', 'pill-idle');
    });

    save.addEventListener('click', async () => {
      save.disabled = true;
      setPill(pill, 'Saving…', 'pill-saving');
      try {
        const resp = await fetch('/api/settings/model', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: select.value }),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.error || `HTTP ${resp.status}`);
        }
        const { model: saved } = await resp.json();
        baseline = saved;
        setPill(pill, 'Saved ✓', 'pill-saved');
        setTimeout(() => setPill(pill, '', 'pill-idle'), 1500);
      } catch (e) {
        setPill(pill, `Error: ${e.message}`, 'pill-error');
        save.disabled = select.value === baseline;
      }
    });
  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error: ${esc(String(e))}</div>`;
  }
}

function setPill(el, text, cls) {
  el.textContent = text;
  el.className = `settings-pill ${cls}`;
}
