import { createProject, validateStep } from './api.js';

const stepDefs = [
  { id: 'identity', title: 'Project' },
  { id: 'tracker',  title: 'Tracker' },
  { id: 'vcs',      title: 'Repository' },
  { id: 'extras',   title: 'Notifications' },
  { id: 'review',   title: 'Review' },
];

const state = {
  step: 0,
  data: {
    identity: {},
    tracker: {
      provider: 'jira',
      jira: {
        statuses: { todo: 'To Do', in_progress: 'In Progress', in_review: 'In Review', done: 'Done' },
        trigger_labels: [], ignore_labels: [],
      },
      trello: {
        trigger_labels: [], ignore_labels: [],
        lists: { todo: '', in_progress: '', in_review: '', done: '' },
        _detected_lists: [],
      },
    },
    vcs: { provider: 'github', github: { default_branch: 'develop', branch_prefix: 'feature', merge_method: 'squash' }, gitlab: {} },
    quality: { lint: {hard_gate: true}, test: {hard_gate: true}, build: {hard_gate: true} },
    extras: { protected_files: [] },
  },
  errors: {},
  running: null,
  validated: { tracker: false, vcs: false, telegram: false },
};

let els;

export function openWizard() {
  els = {
    modal: document.getElementById('project-wizard-modal'),
    steps: document.getElementById('wizard-steps'),
    body: document.getElementById('wizard-body'),
    back: document.getElementById('wizard-back'),
    next: document.getElementById('wizard-next'),
    close: document.getElementById('wizard-close'),
  };
  els.modal.classList.remove('hidden');
  els.back.onclick = onBack;
  els.next.onclick = onNext;
  els.close.onclick = closeWizard;
  state.step = 0;
  render();
}

function closeWizard() {
  els.modal.classList.add('hidden');
}

function render() {
  renderSteps();
  const def = stepDefs[state.step];
  const renderer = renderers[def.id];
  els.body.innerHTML = '';
  renderer(els.body);
  if (state.errors._check) {
    const msg = document.createElement('div');
    msg.className = 'check-gate-msg';
    msg.textContent = state.errors._check;
    els.body.appendChild(msg);
  }
  // Enter key: focus next input, or click Next if last
  const inputs = [...els.body.querySelectorAll('input:not([type=checkbox]), select')];
  inputs.forEach((inp, i) => {
    inp.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Enter') return;
      ev.preventDefault();
      if (i < inputs.length - 1) inputs[i + 1].focus();
      else els.next.click();
    });
  });
  els.back.disabled = state.step === 0;
  els.next.textContent = state.step === stepDefs.length - 1 ? 'Create project' : 'Next';
}

function renderSteps() {
  els.steps.innerHTML = '';
  stepDefs.forEach((def, i) => {
    const el = document.createElement('span');
    el.className = 'step' + (i === state.step ? ' active' : i < state.step ? ' done' : '');
    el.textContent = `${i + 1}. ${def.title}`;
    els.steps.appendChild(el);
  });
}

function onBack() {
  if (state.step > 0) {
    state.step -= 1;
    render();
  }
}

async function onNext() {
  const def = stepDefs[state.step];
  const validator = validators[def.id];
  const errors = validator(state.data[def.id]);
  if (Object.keys(errors).length > 0) {
    state.errors = errors;
    render();
    return;
  }
  // Block steps that require live validation
  if (def.id === 'tracker' && !state.validated.tracker) {
    state.errors = { _check: 'Click "Check connection" and fix any errors before proceeding' };
    render();
    return;
  }
  if (def.id === 'tracker' && state.data.tracker.provider === 'trello') {
    const lists = state.data.tracker.trello.lists;
    if (!lists.todo || !lists.in_progress || !lists.in_review || !lists.done) {
      state.errors = { _check: 'Pick a Trello list for each status before proceeding' };
      render();
      return;
    }
  }
  if (def.id === 'vcs' && !state.validated.vcs) {
    state.errors = { _check: 'Click "Check repository access" before proceeding' };
    render();
    return;
  }
  if (def.id === 'extras' && !state.validated.telegram) {
    state.errors = { _check: 'Click "Test Telegram" before proceeding' };
    render();
    return;
  }
  state.errors = {};
  if (state.step === stepDefs.length - 1) {
    await submit();
  } else {
    state.step += 1;
    render();
  }
}

const SLUG_RE = /^[a-z][a-z0-9-]{0,62}$/;

function mountChipInput(container, values, onChange) {
  const wrap = document.createElement('div');
  wrap.className = 'chip-input';
  const renderChips = () => {
    wrap.innerHTML = '';
    values.forEach((v, idx) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.innerHTML = `${v}<button type="button">×</button>`;
      chip.querySelector('button').onclick = () => {
        values.splice(idx, 1);
        renderChips();
        onChange(values);
      };
      wrap.appendChild(chip);
    });
    const input = document.createElement('input');
    input.placeholder = 'Add label…';
    input.onkeydown = (ev) => {
      if ((ev.key === 'Enter' || ev.key === ',') && input.value.trim()) {
        ev.preventDefault();
        values.push(input.value.trim());
        onChange(values);
        renderChips();
      } else if (ev.key === 'Backspace' && !input.value && values.length) {
        values.pop();
        onChange(values);
        renderChips();
      }
    };
    wrap.appendChild(input);
    input.focus();
  };
  renderChips();
  container.appendChild(wrap);
}

function renderJiraFields(container, d, e) {
  const urlClean = (v) => { const m = v.match(/^(https?:\/\/[^/]+)/); return m ? m[1] : v; };
  container.innerHTML = `
    <h4>Jira</h4>
    <div class="form-field">
      <label>Jira URL</label>
      <input id="f-jira-url" value="${d.url || ''}" placeholder="https://yourteam.atlassian.net" />
      <small class="hint">Just the base URL — board/project path is stripped automatically</small>
      ${e.url ? `<span class="error">${e.url}</span>` : ''}
    </div>
    <div class="form-field"><label>Project key</label><input id="f-jira-key" value="${d.project_key || ''}" placeholder="e.g. PROJ, ACME" />${e.project_key ? `<span class="error">${e.project_key}</span>` : ''}</div>
    <div class="form-field"><label>Email</label><input id="f-jira-email" value="${d.email || ''}" placeholder="your-bot@company.com" /></div>
    <div class="form-field"><label>API token</label><input type="password" id="f-jira-token" value="${d.token || ''}" placeholder="From id.atlassian.com/manage/api-tokens" />${e.token ? `<span class="error">${e.token}</span>` : ''}</div>
    <div class="form-field">
      <label>Trigger labels <small class="hint">comma-separated — ticket must have ALL of these</small></label>
      <input id="f-jira-labels" value="${(d.trigger_labels || []).join(', ')}" placeholder="ai-pipeline, your-repo-label" />
      ${e.trigger_labels ? `<span class="error">${e.trigger_labels}</span>` : ''}
    </div>
    <div class="form-field">
      <label>Ignore labels <small class="hint">comma-separated — tickets with any of these are skipped</small></label>
      <input id="f-jira-ignore" value="${(d.ignore_labels || []).join(', ')}" placeholder="on-hold, manual-only" />
    </div>
    <div class="form-field">
      <button id="f-jira-check" class="btn-check" type="button">Check Jira connection</button>
      <div id="f-jira-check-result"></div>
    </div>
  `;
  container.querySelector('#f-jira-url').oninput = (ev) => d.url = urlClean(ev.target.value);
  container.querySelector('#f-jira-url').onblur = (ev) => { d.url = urlClean(ev.target.value); ev.target.value = d.url; };
  container.querySelector('#f-jira-key').oninput = (ev) => d.project_key = ev.target.value.toUpperCase();
  container.querySelector('#f-jira-email').oninput = (ev) => d.email = ev.target.value;
  container.querySelector('#f-jira-token').oninput = (ev) => d.token = ev.target.value;
  container.querySelector('#f-jira-labels').oninput = (ev) => d.trigger_labels = ev.target.value.split(',').map(s => s.trim()).filter(Boolean);
  container.querySelector('#f-jira-ignore').oninput = (ev) => d.ignore_labels = ev.target.value.split(',').map(s => s.trim()).filter(Boolean);
  const jiraInvalidate = () => { state.validated.tracker = false; };
  for (const id of ['#f-jira-url', '#f-jira-key', '#f-jira-email', '#f-jira-token']) {
    const el = container.querySelector(id);
    if (el) el.addEventListener('input', jiraInvalidate);
  }
  container.querySelector('#f-jira-check').onclick = async () => {
    const btn = container.querySelector('#f-jira-check');
    const res_el = container.querySelector('#f-jira-check-result');
    btn.disabled = true; btn.textContent = 'Checking…';
    res_el.innerHTML = '';
    const r = await validateStep('jira', {
      url: d.url, email: d.email, token: d.token, project_key: d.project_key,
    });
    btn.disabled = false; btn.textContent = 'Check Jira connection';
    if (r.ok) {
      state.validated.tracker = true;
      res_el.innerHTML = '<span class="check-pass">Connected — credentials valid</span>';
    } else {
      state.validated.tracker = false;
      const c = (r.checks || [])[0] || {};
      res_el.innerHTML = `<span class="check-fail">${escapeHtml(c.reason || r.error || 'Check failed')}</span>`
        + (c.fix_hint ? `<br><small class="hint">${escapeHtml(c.fix_hint)}</small>` : '');
    }
  };
}

function renderTrelloFields(container, d, e) {
  const renderColumnPicker = () => {
    if (!d._detected_lists.length) return '';
    const opt = (id, name, selected) => `<option value="${id}" ${selected === id ? 'selected' : ''}>${escapeHtml(name)}</option>`;
    const blank = `<option value="">— pick —</option>`;
    const render_one = (statusKey, label) => `
      <div class="form-field">
        <label>${label}</label>
        <select id="f-trello-list-${statusKey}">
          ${blank}
          ${d._detected_lists.map(l => opt(l.id, l.name, d.lists[statusKey])).join('')}
        </select>
        ${d.lists[statusKey] ? '<small class="check-pass">auto-detected</small>' : '<small class="check-fail">please pick</small>'}
      </div>
    `;
    return `
      <h4>Columns → Cleave statuses</h4>
      ${render_one('todo', 'Todo')}
      ${render_one('in_progress', 'In Progress')}
      ${render_one('in_review', 'In Review')}
      ${render_one('done', 'Done')}
    `;
  };

  container.innerHTML = `
    <h4>Trello</h4>
    <div class="form-field">
      <label>API key</label>
      <input id="f-trello-key" type="password" value="${d.api_key || ''}" placeholder="From trello.com/app-key" />
      <small class="hint"><a href="https://trello.com/app-key" target="_blank" rel="noopener">Get your API key</a></small>
      ${e.api_key ? `<span class="error">${e.api_key}</span>` : ''}
    </div>
    <div class="form-field">
      <label>Token</label>
      <input id="f-trello-token" type="password" value="${d.token || ''}" placeholder="Generated from the API key page" />
    </div>
    <div class="form-field">
      <label>Board URL or short ID</label>
      <input id="f-trello-board" value="${d.board_id || ''}" placeholder="https://trello.com/b/abc123/my-board" />
      <small class="hint">URL or just the short ID (after /b/)</small>
    </div>
    <div class="form-field">
      <label>Trigger labels <small class="hint">comma-separated</small></label>
      <input id="f-trello-labels" value="${(d.trigger_labels || []).join(', ')}" placeholder="ai-pipeline" />
    </div>
    <div class="form-field">
      <label>Ignore labels <small class="hint">comma-separated, optional</small></label>
      <input id="f-trello-ignore" value="${(d.ignore_labels || []).join(', ')}" placeholder="wip, blocked" />
    </div>
    <div class="form-field">
      <button id="f-trello-check" class="btn-check" type="button">Validate & fetch columns</button>
      <div id="f-trello-check-result"></div>
    </div>
    <div id="f-trello-cols">${renderColumnPicker()}</div>
  `;

  const invalidate = () => {
    state.validated.tracker = false;
    d._detected_lists = [];
    d.lists = { todo: '', in_progress: '', in_review: '', done: '' };
    render();
  };
  container.querySelector('#f-trello-key').oninput = (ev) => { d.api_key = ev.target.value; invalidate(); };
  container.querySelector('#f-trello-token').oninput = (ev) => { d.token = ev.target.value; invalidate(); };
  container.querySelector('#f-trello-board').oninput = (ev) => {
    let v = ev.target.value.trim();
    const m = v.match(/trello\.com\/b\/([A-Za-z0-9]+)/);
    if (m) v = m[1];
    d.board_id = v;
    invalidate();
  };
  container.querySelector('#f-trello-labels').oninput = (ev) => d.trigger_labels = ev.target.value.split(',').map(s => s.trim()).filter(Boolean);
  container.querySelector('#f-trello-ignore').oninput = (ev) => d.ignore_labels = ev.target.value.split(',').map(s => s.trim()).filter(Boolean);
  for (const k of ['todo', 'in_progress', 'in_review', 'done']) {
    const sel = container.querySelector(`#f-trello-list-${k}`);
    if (sel) sel.onchange = (ev) => { d.lists[k] = ev.target.value; render(); };
  }

  container.querySelector('#f-trello-check').onclick = async () => {
    const btn = container.querySelector('#f-trello-check');
    const res_el = container.querySelector('#f-trello-check-result');
    btn.disabled = true; btn.textContent = 'Checking…';
    res_el.innerHTML = '';
    const r = await validateStep('trello', {
      api_key: d.api_key, token: d.token, board_id: d.board_id,
    });
    btn.disabled = false; btn.textContent = 'Validate & fetch columns';
    if (r.ok) {
      state.validated.tracker = true;
      d._detected_lists = r.lists || [];
      const { autodetectStatusMapping } = await import('./trello-autodetect.js');
      const mapping = autodetectStatusMapping(d._detected_lists);
      d.lists = {
        todo: mapping.todo || '',
        in_progress: mapping.in_progress || '',
        in_review: mapping.in_review || '',
        done: mapping.done || '',
      };
      res_el.innerHTML = `<span class="check-pass">Connected — ${d._detected_lists.length} lists fetched, ${Object.values(d.lists).filter(Boolean).length} auto-detected</span>`;
      render();
    } else {
      state.validated.tracker = false;
      const c = (r.checks || [])[0] || {};
      res_el.innerHTML = `<span class="check-fail">${escapeHtml(c.reason || r.error || 'Check failed')}</span>`
        + (c.fix_hint ? `<br><small class="hint">${escapeHtml(c.fix_hint)}</small>` : '');
    }
  };
}

const renderers = {
  identity(body) {
    const d = state.data.identity;
    const e = state.errors;
    const slugify = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 63);
    body.innerHTML = `
      <h3>Project</h3>
      <div class="form-field">
        <label>Project name</label>
        <input id="f-display-name" value="${d.display_name || ''}" placeholder="e.g. Acme Corp, Globex Inc" autofocus />
        ${e.display_name ? `<span class="error">${e.display_name}</span>` : ''}
      </div>
    `;
    body.querySelector('#f-display-name').oninput = (ev) => {
      d.display_name = ev.target.value;
      d.project_id = slugify(ev.target.value);
    };
  },
  tracker(body) {
    const d = state.data.tracker;
    const e = state.errors;
    body.innerHTML = `
      <h3>Tracker</h3>
      <div class="form-field">
        <label>Provider</label>
        <label style="margin-right: 1em;"><input type="radio" name="tracker-provider" value="jira"   ${d.provider === 'jira' ? 'checked' : ''}/> Jira</label>
        <label><input type="radio" name="tracker-provider" value="trello" ${d.provider === 'trello' ? 'checked' : ''}/> Trello</label>
      </div>
      <div id="tracker-fields"></div>
    `;
    body.querySelectorAll('input[name=tracker-provider]').forEach((el) => {
      el.onchange = (ev) => {
        d.provider = ev.target.value;
        state.validated.tracker = false;
        render();
      };
    });
    const fields = body.querySelector('#tracker-fields');
    if (d.provider === 'jira') renderJiraFields(fields, d.jira, e);
    else renderTrelloFields(fields, d.trello, e);
  },
  vcs(body) {
    const d = state.data.vcs;
    const e = state.errors;

    const parseRepoUrl = (url) => {
      url = url.trim().replace(/\.git$/, '').replace(/\/$/, '');
      const ghMatch = url.match(/github\.com[/:]([^/]+)\/([^/]+)/);
      if (ghMatch) return { provider: 'github', owner: ghMatch[1], repo: ghMatch[2] };
      const glMatch = url.match(/^(https?:\/\/[^/]+)\/(.+?)(?:\/)?$/);
      if (glMatch && !url.includes('github.com')) return { provider: 'gitlab', url: glMatch[1], path: glMatch[2] };
      return null;
    };

    const currentUrl = d.provider === 'github' && d.github.owner
      ? `https://github.com/${d.github.owner}/${d.github.repo || ''}`
      : d.provider === 'gitlab' && d.gitlab.url && d.gitlab.project_id
        ? `${d.gitlab.url}/${d.gitlab.project_id}`
        : '';

    const detected = d.provider === 'github' && d.github.owner
      ? `<span class="hint">GitHub — ${d.github.owner}/${d.github.repo || '?'}</span>`
      : d.provider === 'gitlab' && d.gitlab.project_id
        ? `<span class="hint">GitLab — ${d.gitlab.project_id}</span>`
        : '';

    const githubFields = `
      <div class="form-field"><label>Token (PAT with repo scope)</label><input type="password" id="f-gh-token" value="${d.github.token || ''}" placeholder="ghp_..." />${e.token ? `<span class="error">${e.token}</span>` : ''}</div>
      <details class="advanced-toggle"><summary>Advanced settings</summary>
        <div class="form-field"><label>Default branch</label><input id="f-gh-branch" value="${d.github.default_branch}" placeholder="develop" /></div>
        <div class="form-field"><label>Branch prefix</label><input id="f-gh-prefix" value="${d.github.branch_prefix}" placeholder="feature" /></div>
        <div class="form-field"><label>Merge method</label>
          <select id="f-gh-merge">
            <option value="squash" ${d.github.merge_method === 'squash' ? 'selected' : ''}>squash</option>
            <option value="merge" ${d.github.merge_method === 'merge' ? 'selected' : ''}>merge</option>
            <option value="rebase" ${d.github.merge_method === 'rebase' ? 'selected' : ''}>rebase</option>
          </select>
        </div>
      </details>
    `;
    const gitlabFields = `
      <div class="form-field"><label>Token</label><input type="password" id="f-gl-token" value="${d.gitlab.token || ''}" />${e.token ? `<span class="error">${e.token}</span>` : ''}</div>
      <details class="advanced-toggle"><summary>Advanced settings</summary>
        <div class="form-field"><label>Default branch</label><input id="f-gl-branch" value="${d.gitlab.default_branch || 'develop'}" /></div>
        <div class="form-field"><label>Branch prefix</label><input id="f-gl-prefix" value="${d.gitlab.branch_prefix || 'feature'}" /></div>
      </details>
    `;
    const providerFields = d.provider === 'github' ? githubFields : d.provider === 'gitlab' ? gitlabFields : '';

    body.innerHTML = `
      <h3>Repository</h3>
      <div class="form-field">
        <label>Repository URL</label>
        <input id="f-repo-url" value="${currentUrl}" placeholder="https://github.com/owner/repo" />
        ${detected}
        ${e.repo_url ? `<span class="error">${e.repo_url}</span>` : ''}
      </div>
      <div id="vcs-provider-fields">${providerFields}</div>
      ${d.provider ? `<div class="form-field">
        <button id="f-vcs-check" class="btn-check" type="button">Check repository access</button>
        <div id="f-vcs-check-result"></div>
      </div>` : ''}
    `;

    body.querySelector('#f-repo-url').oninput = (ev) => {
      const parsed = parseRepoUrl(ev.target.value);
      if (parsed) {
        d.provider = parsed.provider;
        const id = state.data.identity;
        if (parsed.provider === 'github') {
          d.github.owner = parsed.owner;
          d.github.repo = parsed.repo;
          id.repo_id = id.repo_id || parsed.repo;
          id.repo_display_name = id.repo_display_name || parsed.repo;
        } else {
          d.gitlab.url = parsed.url;
          d.gitlab.project_id = parsed.path;
          const slug = parsed.path.split('/').pop();
          id.repo_id = id.repo_id || slug;
          id.repo_display_name = id.repo_display_name || slug;
        }
        renderers.vcs(body);
        body.querySelector('#f-repo-url').value = ev.target.value;
      }
    };

    if (d.provider === 'github') {
      body.querySelector('#f-gh-token').oninput = (ev) => d.github.token = ev.target.value;
      body.querySelector('#f-gh-branch').oninput = (ev) => d.github.default_branch = ev.target.value;
      body.querySelector('#f-gh-prefix').oninput = (ev) => d.github.branch_prefix = ev.target.value;
      body.querySelector('#f-gh-merge').onchange = (ev) => d.github.merge_method = ev.target.value;
    } else if (d.provider === 'gitlab') {
      body.querySelector('#f-gl-token').oninput = (ev) => d.gitlab.token = ev.target.value;
      body.querySelector('#f-gl-branch').oninput = (ev) => d.gitlab.default_branch = ev.target.value;
      body.querySelector('#f-gl-prefix').oninput = (ev) => d.gitlab.branch_prefix = ev.target.value;
    }
    const vcsCheckBtn = body.querySelector('#f-vcs-check');
    if (vcsCheckBtn) {
      vcsCheckBtn.onclick = async () => {
        const res_el = body.querySelector('#f-vcs-check-result');
        vcsCheckBtn.disabled = true; vcsCheckBtn.textContent = 'Checking…';
        res_el.innerHTML = '';
        const checkData = d.provider === 'github'
          ? { provider: 'github', token: d.github.token, owner: d.github.owner, repo: d.github.repo }
          : { provider: 'gitlab', token: d.gitlab.token, project_id: d.gitlab.project_id, url: d.gitlab.url };
        const r = await validateStep('vcs', checkData);
        vcsCheckBtn.disabled = false; vcsCheckBtn.textContent = 'Check repository access';
        if (r.ok) {
          state.validated.vcs = true;
          res_el.innerHTML = '<span class="check-pass">Repository accessible — credentials valid</span>';
        } else {
          state.validated.vcs = false;
          const c = (r.checks || [])[0] || {};
          res_el.innerHTML = `<span class="check-fail">${escapeHtml(c.reason || r.error || 'Check failed')}</span>`
            + (c.fix_hint ? `<br><small class="hint">${escapeHtml(c.fix_hint)}</small>` : '');
        }
      };
    }
  },
  quality(body) {
    const d = state.data.quality;
    const row = (key, label) => `
      <div class="form-field">
        <label>${label} command</label>
        <input id="f-q-${key}-cmd" value="${d[key].command || ''}" placeholder="optional" />
        <label><input type="checkbox" id="f-q-${key}-gate" ${d[key].hard_gate ? 'checked' : ''}/> Hard gate</label>
      </div>
    `;
    body.innerHTML = `
      <h3>Quality gates</h3>
      <p class="hint">Commands the pipeline runs after code changes to verify quality. Leave blank to skip a gate. "Hard gate" means the pipeline stops if the command fails.</p>
      ${row('lint', 'Lint')}${row('test', 'Test')}${row('build', 'Build')}`;
    for (const key of ['lint', 'test', 'build']) {
      body.querySelector(`#f-q-${key}-cmd`).oninput = (ev) => d[key].command = ev.target.value;
      body.querySelector(`#f-q-${key}-gate`).onchange = (ev) => d[key].hard_gate = ev.target.checked;
    }
  },
  async extras(body) {
    const d = state.data.extras;
    const e = state.errors;
    let hasGlobal = false;
    try { const r = await (await fetch('/api/projects/telegram-globals')).json(); hasGlobal = r.has_global; } catch {}
    const tgRequired = !hasGlobal;
    state._tgRequired = tgRequired;
    const tgHint = hasGlobal
      ? 'Global Telegram bot configured — leave blank to use it.'
      : 'No global Telegram config. Enter bot token and chat ID.';
    body.innerHTML = `
      <h3>Notifications</h3>
      <p class="hint">${tgHint}</p>
      <div class="form-field">
        <label>Telegram bot token ${tgRequired ? '' : '<small class="hint">optional</small>'}</label>
        <input type="password" id="f-ex-tg-token" value="${d.telegram_bot_token || ''}" placeholder="From @BotFather" />
        ${e.telegram_bot_token ? `<span class="error">${e.telegram_bot_token}</span>` : ''}
      </div>
      <div class="form-field">
        <label>Telegram chat ID ${tgRequired ? '' : '<small class="hint">optional</small>'}</label>
        <input id="f-ex-tg-chat" value="${d.telegram_chat_id || ''}" placeholder="From @userinfobot" />
        ${e.telegram_chat_id ? `<span class="error">${e.telegram_chat_id}</span>` : ''}
      </div>
      <div class="form-field">
        <button id="f-tg-check" class="btn-check" type="button">Test Telegram</button>
        <div id="f-tg-check-result"></div>
      </div>
    `;
    body.querySelector('#f-ex-tg-token').oninput = (ev) => { d.telegram_bot_token = ev.target.value || null; state.validated.telegram = false; };
    body.querySelector('#f-ex-tg-chat').oninput = (ev) => { d.telegram_chat_id = ev.target.value || null; state.validated.telegram = false; };
    body.querySelector('#f-tg-check').onclick = async () => {
      const btn = body.querySelector('#f-tg-check');
      const res_el = body.querySelector('#f-tg-check-result');
      btn.disabled = true; btn.textContent = 'Sending…';
      res_el.innerHTML = '';
      const r = await validateStep('telegram', { token: d.telegram_bot_token || '', chat_id: d.telegram_chat_id || '' });
      btn.disabled = false; btn.textContent = 'Test Telegram';
      if (r.ok) {
        state.validated.telegram = true;
        res_el.innerHTML = '<span class="check-pass">Message sent — check your Telegram</span>';
      } else {
        state.validated.telegram = false;
        const c = (r.checks || [])[0] || {};
        res_el.innerHTML = `<span class="check-fail">${escapeHtml(c.reason || r.error || 'Failed')}</span>`
          + (c.fix_hint ? `<br><small class="hint">${escapeHtml(c.fix_hint)}</small>` : '');
      }
    };
  },
  review(body) {
    const d = state.data;
    const prefix = (d.identity.project_id || '').toUpperCase().replace(/-/g, '_');
    const env = (name) => `<code>${prefix}_${name}</code>`;
    const vp = d.vcs.provider;
    const repo = vp === 'github'
      ? `${d.vcs.github.owner}/${d.vcs.github.repo}`
      : d.vcs.gitlab.project_id || '';
    const tg = d.extras.telegram_bot_token
      ? 'Project-specific bot configured'
      : 'Using global bot from .env';
    body.innerHTML = `
      <h3>Review before creating</h3>
      <table class="review-table">
        <tr><th colspan="2">Project</th></tr>
        <tr><td>Name</td><td><strong>${escapeHtml(d.identity.display_name)}</strong> <small class="hint">(${d.identity.project_id})</small></td></tr>
        <tr><td>Repository</td><td>${escapeHtml(d.identity.repo_display_name || d.identity.repo_id || '—')} <small class="hint">(${d.identity.repo_id || '—'})</small></td></tr>
        <tr><th colspan="2">Tracker (${escapeHtml(d.tracker.provider)})</th></tr>
        ${d.tracker.provider === 'jira' ? `
        <tr><td>URL</td><td>${escapeHtml(d.tracker.jira.url || '—')}</td></tr>
        <tr><td>Project</td><td>${escapeHtml(d.tracker.jira.project_key || '—')}</td></tr>
        <tr><td>Email</td><td>${escapeHtml(d.tracker.jira.email || '—')}</td></tr>
        <tr><td>Labels</td><td>${(d.tracker.jira.trigger_labels || []).map(l => `<span class="review-label">${escapeHtml(l)}</span>`).join(' ')}</td></tr>
        <tr><td>Token</td><td>•••• → ${env('JIRA_TOKEN')}</td></tr>
        ` : `
        <tr><td>Board ID</td><td>${escapeHtml(d.tracker.trello.board_id || '—')}</td></tr>
        <tr><td>Labels</td><td>${(d.tracker.trello.trigger_labels || []).map(l => `<span class="review-label">${escapeHtml(l)}</span>`).join(' ')}</td></tr>
        <tr><td>API key</td><td>•••• → ${env('TRELLO_KEY')}</td></tr>
        <tr><td>Token</td><td>•••• → ${env('TRELLO_TOKEN')}</td></tr>
        `}
        <tr><th colspan="2">Repository (${vp})</th></tr>
        <tr><td>Repo</td><td>${escapeHtml(repo)}</td></tr>
        <tr><td>Branch</td><td>${escapeHtml(vp === 'github' ? d.vcs.github.default_branch : d.vcs.gitlab.default_branch || 'develop')}</td></tr>
        <tr><td>Token</td><td>•••• → ${env(vp === 'github' ? 'GITHUB_TOKEN' : 'GITLAB_TOKEN')}</td></tr>
        <tr><th colspan="2">Notifications</th></tr>
        <tr><td>Telegram</td><td>${tg}</td></tr>
      </table>
      <p class="hint" style="margin-top:12px;">Quality gates and advanced settings can be configured later in the project YAML.</p>
    `;
  },
};

const validators = {
  identity(d) {
    const errors = {};
    if (!d.display_name) errors.display_name = 'required';
    return errors;
  },
  tracker(d) {
    const errors = {};
    if (d.provider === 'jira') {
      const j = d.jira;
      if (!j.url) errors.url = 'required';
      if (!j.project_key) errors.project_key = 'required';
      if (!j.token) errors.token = 'required';
      if (!j.trigger_labels || j.trigger_labels.length === 0) errors.trigger_labels = 'at least one label required';
    } else if (d.provider === 'trello') {
      const t = d.trello;
      if (!t.api_key) errors.api_key = 'required';
      if (!t.token) errors.token = 'required';
      if (!t.board_id) errors.board_id = 'required';
    }
    return errors;
  },
  vcs(d) {
    const errors = {};
    if (!d.provider) { errors.repo_url = 'Paste a GitHub or GitLab repo URL'; return errors; }
    if (d.provider === 'github') {
      if (!d.github.owner || !d.github.repo) errors.repo_url = 'Could not parse owner/repo from URL';
      if (!d.github.token) errors.token = 'required';
    } else {
      if (!d.gitlab.url || !d.gitlab.project_id) errors.repo_url = 'Could not parse GitLab project from URL';
      if (!d.gitlab.token) errors.token = 'required';
    }
    return errors;
  },
  quality() { return {}; },
  extras(d) {
    const errors = {};
    if (state._tgRequired) {
      const ex = state.data.extras;
      if (!ex.telegram_bot_token) errors.telegram_bot_token = 'required — no global Telegram config';
      if (!ex.telegram_chat_id) errors.telegram_chat_id = 'required — no global Telegram config';
    }
    return errors;
  },
  review() { return {}; },
};

async function submit() {
  const payload = buildPayload();
  els.body.innerHTML = '<div class="status-panel"><p>Submitting…</p></div>';
  els.back.style.display = 'none';
  els.next.style.display = 'none';
  const { status, body } = await createProject(payload);
  if (status !== 202) {
    renderSubmitError(status, body);
    els.back.style.display = '';
    els.next.style.display = '';
    els.next.disabled = false;
    return;
  }
  state.running = body;
  pollStatus();
}

function buildPayload() {
  const d = state.data;
  const vcs = { provider: d.vcs.provider };
  if (d.vcs.provider === 'github') vcs.github = { ...d.vcs.github };
  else vcs.gitlab = { ...d.vcs.gitlab };
  const tracker = { provider: d.tracker.provider };
  if (d.tracker.provider === 'jira') {
    tracker.jira = { ...d.tracker.jira, trigger_labels: [...d.tracker.jira.trigger_labels], ignore_labels: [...d.tracker.jira.ignore_labels] };
  } else {
    tracker.trello = { ...d.tracker.trello, trigger_labels: [...d.tracker.trello.trigger_labels], ignore_labels: [...d.tracker.trello.ignore_labels] };
  }
  return {
    identity: { ...d.identity },
    tracker,
    vcs,
    quality: { ...d.quality },
    extras: { ...d.extras, protected_files: [...d.extras.protected_files] },
  };
}

async function renderFailure(entry) {
  let report = '';
  try {
    const res = await fetch(`/api/workspaces/${encodeURIComponent(entry.ticket_id)}/report/project-setup-output.md`);
    report = await res.text();
  } catch {}
  // Extract fix hints from agent output (lines starting with digits or "Fix hint")
  const hints = report.split('\n')
    .filter(l => /^\d+\.\s|^-\s\*\*FAIL|fix hint/i.test(l.trim()))
    .map(l => `<li>${escapeHtml(l.trim().replace(/^\d+\.\s*/, ''))}</li>`)
    .join('');
  els.body.innerHTML = `
    <div class="status-panel failed">
      <h3>Setup failed</h3>
      ${hints ? `<div style="text-align:left;margin-bottom:12px;"><strong>What to fix:</strong><ul>${hints}</ul></div>` : ''}
      <details style="text-align:left;margin-bottom:12px;">
        <summary>Full agent report</summary>
        <pre style="white-space:pre-wrap;max-height:300px;overflow:auto;font-size:12px;">${escapeHtml(report)}</pre>
      </details>
      <p class="hint">Fix the issues above, then click retry. Secrets are cleared — you'll re-enter tokens.</p>
      <button id="retry-btn" class="btn-primary">Edit & retry</button>
    </div>
  `;
  document.getElementById('retry-btn').onclick = () => {
    state.data.tracker.jira.token = '';
    state.data.tracker.trello.token = '';
    if (state.data.vcs.provider === 'github') state.data.vcs.github.token = '';
    else state.data.vcs.gitlab.token = '';
    state.data.extras.telegram_bot_token = null;
    state.running = null;
    state.step = 0;
    els.next.disabled = false;
    render();
  };
}

async function renderSuccess() {
  const d = state.data;
  const pid = d.identity.project_id;
  const vcs = d.vcs.provider === 'github'
    ? `${d.vcs.github.owner}/${d.vcs.github.repo}`
    : d.vcs.gitlab.project_id;

  let mode = 'unknown';
  try { const r = await (await fetch('/api/daemon/status')).json(); mode = r.mode; } catch {}

  const modeMsg = mode === 'manual'
    ? `<div class="mode-notice warning">
        <strong>Daemon is in manual mode.</strong> Tickets won't be fetched automatically.
        Switch to auto mode to start processing:
        <button id="success-switch-auto" class="btn-check" style="margin-left:8px;">Switch to auto</button>
       </div>`
    : `<div class="mode-notice ok">Daemon is in auto mode — tickets will be fetched on the next poll cycle.</div>`;

  els.body.innerHTML = `
    <div class="status-panel success">
      <h3>Project created</h3>
      <ul style="text-align:left;">
        <li><strong>${d.identity.display_name}</strong> (${pid})</li>
        <li>Tracker: ${d.tracker.provider === 'jira' ? `Jira: ${d.tracker.jira.project_key} — ${d.tracker.jira.trigger_labels.join(', ')}` : `Trello board: ${d.tracker.trello.board_id || '—'}`}</li>
        <li>VCS: ${d.vcs.provider} — ${vcs}</li>
      </ul>
      ${modeMsg}
      <div style="display:flex;gap:8px;justify-content:center;margin-top:16px;">
        <button id="success-close" class="btn-primary">Go to board</button>
      </div>
    </div>
  `;
  document.getElementById('success-close').onclick = () => { closeWizard(); };
  const switchBtn = document.getElementById('success-switch-auto');
  if (switchBtn) {
    switchBtn.onclick = async () => {
      await fetch('/api/daemon/mode', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({mode:'auto'}) });
      switchBtn.textContent = 'Switched to auto';
      switchBtn.disabled = true;
    };
  }
  els.back.style.display = 'none';
  els.next.style.display = 'none';
}

function renderSubmitError(status, body) {
  els.body.innerHTML = `
    <div class="status-panel failed">
      <h3>Error ${status}</h3>
      <pre>${escapeHtml(JSON.stringify(body, null, 2))}</pre>
      <button id="back-to-form" class="btn-primary">Back to form</button>
    </div>
  `;
  document.getElementById('back-to-form').onclick = () => {
    state.step = 0;
    els.next.disabled = false;
    render();
  };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function pollStatus() {
  const { workspace } = state.running;
  els.body.innerHTML = `
    <div class="status-panel">
      <h3>Setting up…</h3>
      <div>
        <span class="status-step" id="st-validating">VALIDATING</span>
        <span class="status-step" id="st-writing">WRITING</span>
        <span class="status-step" id="st-done">DONE</span>
      </div>
      <p id="st-message"></p>
    </div>
  `;
  const project = workspace.split('/')[0];
  let poll;
  const tick = async () => {
    const res = await fetch(`/api/workspaces?project_id=${project}`);
    const body = await res.json();
    const entry = (body.workspaces || []).find(
      (w) => w.workspace_root && w.workspace_root.endsWith('/setup')
        && w.company_id === project,
    );
    if (!entry) return;
    const st = entry.current_state;
    const active = (id, on) => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle('active', on);
    };
    const done = (id, on) => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle('done', on);
    };
    if (st === 'VALIDATING') { active('st-validating', true); }
    if (st === 'WRITING') { done('st-validating', true); active('st-writing', true); }
    if (st === 'SETUP_DONE') {
      done('st-validating', true); done('st-writing', true); done('st-done', true);
      clearInterval(poll);
      renderSuccess();
      window.dispatchEvent(new CustomEvent('cleave:projects-changed'));
    }
    if (st === 'SETUP_FAILED') {
      clearInterval(poll);
      await renderFailure(entry);
    }
  };
  poll = setInterval(tick, 2000);
  tick();
}

// Expose for Task 17+ module additions.
export const _internal = { state, stepDefs, renderers, validators };
