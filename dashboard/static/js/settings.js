// settings.js — Settings view (model picker, Telegram language)

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
        <div id="settings-language" class="settings-language"></div>
      </section>
    `;
    renderLanguage(document.getElementById('settings-language'));

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

const LANGUAGE_NAMES = { en: 'English', uk: 'Українська' };

// The language the bot writes in Telegram: global.yaml's (messages tied to no project) and each project's.
// A save writes telegram.language into that YAML file and applies at once.
async function renderLanguage(el) {
  try {
    const r = await fetch('/api/settings/telegram-language');
    if (!r.ok) throw new Error(`GET failed: ${r.status}`);
    const { options, global, projects } = await r.json();
    const rows = [{ id: null, label: 'Global (no project)', value: global },
      ...projects.map(p => ({ id: p.id, label: `${p.name} (${p.id})`, value: p.effective }))];
    el.innerHTML = `
      <h2 class="settings-section-title">Telegram language</h2>
      <p class="settings-help">The language the bot writes in Telegram for each project, including what models wrote (translated when it is sent). GitHub and the dashboard stay in English; reply commands stay as they are. A project without its own setting follows the global one. Saved into the project's <code>project.yaml</code> (<code>global.yaml</code> for the global one); applies to the next message.</p>
      ${rows.map((row, i) => `
        <div class="settings-row">
          <label class="settings-label settings-label-wide" for="tg-lang-${i}">${esc(row.label)}</label>
          <select id="tg-lang-${i}" class="settings-spinner">${options.map(o =>
            `<option value="${esc(o)}"${o === row.value ? ' selected' : ''}>${esc(LANGUAGE_NAMES[o] || o)}</option>`).join('')}</select>
          <button id="tg-lang-save-${i}" class="btn-primary" disabled>Save</button>
          <span id="tg-lang-status-${i}" class="settings-pill pill-idle"></span>
        </div>`).join('')}
    `;
    rows.forEach((row, i) => {
      const select = document.getElementById(`tg-lang-${i}`);
      const save = document.getElementById(`tg-lang-save-${i}`);
      const pill = document.getElementById(`tg-lang-status-${i}`);
      let baseline = row.value;
      select.addEventListener('change', () => {
        save.disabled = select.value === baseline;
        setPill(pill, '', 'pill-idle');
      });
      save.addEventListener('click', async () => {
        save.disabled = true;
        setPill(pill, 'Saving…', 'pill-saving');
        try {
          const resp = await fetch('/api/settings/telegram-language', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project_id: row.id, language: select.value }),
          });
          if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.error || `HTTP ${resp.status}`);
          }
          baseline = (await resp.json()).language;
          setPill(pill, 'Saved ✓', 'pill-saved');
          setTimeout(() => setPill(pill, '', 'pill-idle'), 1500);
          // Projects without their own setting follow the global one: show what they use now.
          if (row.id === null) renderLanguage(el);
        } catch (e) {
          setPill(pill, `Error: ${e.message}`, 'pill-error');
          save.disabled = select.value === baseline;
        }
      });
    });
  } catch (e) {
    el.innerHTML = `<div class="state-msg" style="color:#f85149;">Telegram language: ${esc(String(e))}</div>`;
  }
}

function setPill(el, text, cls) {
  el.textContent = text;
  el.className = `settings-pill ${cls}`;
}
