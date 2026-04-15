import { createProject } from './api.js';

const stepDefs = [
  { id: 'identity', title: 'Identity' },
  { id: 'jira',     title: 'Jira' },
  { id: 'vcs',      title: 'VCS' },
  { id: 'quality',  title: 'Quality' },
  { id: 'extras',   title: 'Extras' },
  { id: 'review',   title: 'Review' },
];

const state = {
  step: 0,
  data: {
    identity: {},
    jira: { statuses: { todo: 'To Do', in_progress: 'In Progress', in_review: 'In Review', done: 'Done' }, trigger_labels: [], ignore_labels: [] },
    vcs: { provider: 'github', github: { default_branch: 'develop', branch_prefix: 'feature', merge_method: 'squash' }, gitlab: {} },
    quality: { lint: {hard_gate: true}, test: {hard_gate: true}, build: {hard_gate: true} },
    extras: { protected_files: [] },
  },
  errors: {},
  running: null,
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
  state.errors = {};
  if (state.step === stepDefs.length - 1) {
    await submit();
  } else {
    state.step += 1;
    render();
  }
}

const SLUG_RE = /^[a-z][a-z0-9-]{0,62}$/;

const renderers = {
  identity(body) {
    const d = state.data.identity;
    const e = state.errors;
    body.innerHTML = `
      <h3>Identity</h3>
      <div class="form-field">
        <label>Project ID (slug)</label>
        <input id="f-project-id" value="${d.project_id || ''}" placeholder="acme" />
        ${e.project_id ? `<span class="error">${e.project_id}</span>` : ''}
      </div>
      <div class="form-field">
        <label>Display name</label>
        <input id="f-display-name" value="${d.display_name || ''}" placeholder="Acme Corp" />
        ${e.display_name ? `<span class="error">${e.display_name}</span>` : ''}
      </div>
      <div class="form-field">
        <label>Repo ID (slug)</label>
        <input id="f-repo-id" value="${d.repo_id || ''}" placeholder="acme-app" />
        ${e.repo_id ? `<span class="error">${e.repo_id}</span>` : ''}
      </div>
      <div class="form-field">
        <label>Repo display name</label>
        <input id="f-repo-display-name" value="${d.repo_display_name || ''}" />
        ${e.repo_display_name ? `<span class="error">${e.repo_display_name}</span>` : ''}
      </div>
    `;
    body.querySelector('#f-project-id').oninput = (ev) => d.project_id = ev.target.value;
    body.querySelector('#f-display-name').oninput = (ev) => d.display_name = ev.target.value;
    body.querySelector('#f-repo-id').oninput = (ev) => d.repo_id = ev.target.value;
    body.querySelector('#f-repo-display-name').oninput = (ev) => d.repo_display_name = ev.target.value;
  },
};

const validators = {
  identity(d) {
    const errors = {};
    if (!d.project_id) errors.project_id = 'required';
    else if (!SLUG_RE.test(d.project_id)) errors.project_id = 'must be a lowercase slug';
    if (!d.display_name) errors.display_name = 'required';
    if (!d.repo_id) errors.repo_id = 'required';
    else if (!SLUG_RE.test(d.repo_id)) errors.repo_id = 'must be a lowercase slug';
    if (!d.repo_display_name) errors.repo_display_name = 'required';
    return errors;
  },
};

async function submit() {
  const payload = buildPayload();
  els.body.innerHTML = '<div class="status-panel"><p>Submitting…</p></div>';
  els.back.disabled = true;
  els.next.disabled = true;
  const { status, body } = await createProject(payload);
  if (status !== 202) {
    renderSubmitError(status, body);
    els.next.disabled = false;
    return;
  }
  state.running = body;
  pollStatus();
}

function buildPayload() {
  // Assembled in Task 21 — this stub lets tests import the module.
  return state.data;
}

function renderSubmitError(status, body) {
  // Populated in Task 22.
  els.body.innerHTML = `<div class="status-panel failed"><p>Error ${status}: ${JSON.stringify(body)}</p></div>`;
}

function pollStatus() {
  // Populated in Task 21.
}

// Expose for Task 17+ module additions.
export const _internal = { state, stepDefs, renderers, validators };
