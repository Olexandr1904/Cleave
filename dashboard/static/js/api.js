// api.js — all fetch calls to the backend

export async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function fetchText(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.text();
}

export async function postJSON(url, body = {}) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok && !data.status) {
    throw new Error(data.message || `HTTP ${resp.status}`);
  }
  return data;
}

export async function loadWorkspaces(projectId) {
  let url = '/api/workspaces';
  if (projectId) url += `?project_id=${encodeURIComponent(projectId)}`;
  const data = await fetchJSON(url);
  return data.workspaces || [];
}

export async function loadEvents(opts = {}) {
  let url = '/api/events?limit=' + (opts.limit || 200);
  if (opts.projectId) url += `&project_id=${encodeURIComponent(opts.projectId)}`;
  if (opts.ticketId) url += `&ticket_id=${encodeURIComponent(opts.ticketId)}`;
  const data = await fetchJSON(url);
  return data.events || [];
}

export async function loadDaemonStatus() {
  return fetchJSON('/api/daemon/status');
}

export async function loadReport(ticketId, filename, folder) {
  return fetchText(
    `/api/workspaces/${encodeURIComponent(ticketId)}/report/${encodeURIComponent(filename)}?folder=${encodeURIComponent(folder)}`
  );
}

export async function loadHealth(force = false) {
  const qs = force ? '?refresh=1' : '';
  const resp = await fetch(`/api/health/credentials${qs}`);
  if (!resp.ok) throw new Error(`health fetch failed: ${resp.status}`);
  return resp.json();
}

export async function validateStep(step, data) {
  let res;
  try {
    res = await fetch('/api/projects/validate-step', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ step, data }),
    });
  } catch (e) {
    return { ok: false, error: `Cannot reach server: ${e.message}. Is the daemon running?` };
  }
  if (!res.ok) {
    return { ok: false, error: `Server returned HTTP ${res.status}. Try restarting the daemon.` };
  }
  return res.json().catch(() => ({ ok: false, error: 'Invalid response from server' }));
}

export async function createProject(payload) {
  const res = await fetch('/api/projects/create', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  return { status: res.status, body };
}

export async function loadSwitches() {
  return fetchJSON('/api/switches');
}

export async function setSwitch(mode, state, scope = '*') {
  const resp = await fetch('/api/switches', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, state, scope }),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

export async function loadReviews() {
  const resp = await fetch('/api/reviews');
  // The review routes exist only when some repo has reviews enabled.
  if (resp.status === 404) return { repos: [] };
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function loadComparisons() {
  const resp = await fetch('/api/comparisons');
  // The comparison route exists only when the skill flow runs.
  if (resp.status === 404) return { rows: [], tally: { skill: 0, staged: 0, neither: 0, undecided: 0 } };
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

export async function loadReviewDetail(scope, number) {
  const qs = `scope=${encodeURIComponent(scope)}&number=${encodeURIComponent(number)}`;
  const resp = await fetch(`/api/reviews/pr?${qs}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

async function postReviewAction(url, scope, number) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope, number }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

export async function reviewNow(scope, number) {
  return postReviewAction('/api/reviews/review-now', scope, number);
}

export async function skipReview(scope, number) {
  return postReviewAction('/api/reviews/skip', scope, number);
}

export async function fixNow(scope, number) {
  return postReviewAction('/api/reviews/fix-now', scope, number);
}

export async function fixFindings(scope, number) {
  return postReviewAction('/api/reviews/fix-findings', scope, number);
}

// These two can push to GitHub or post a public review: JSON, same-origin, and the caller waits for the answer.
// An unexpected server error is a plain-text 500, so the body is read as text first.
async function postReview(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const raw = await resp.text();
  let data = null;
  try {
    data = JSON.parse(raw);
  } catch { /* not JSON */ }
  if (!resp.ok) throw new Error((data && data.error) || raw.trim().slice(0, 200) || `HTTP ${resp.status}`);
  if (!data) throw new Error(`HTTP ${resp.status}: the answer is not JSON`);
  return data;
}

// A board move on GitHub: {move: 'request_review', reviewers} | {move: 'ready'} | {move: 'merge', method, head}.
export async function prMove(scope, number, body) {
  return postReview('/api/reviews/pr-move', { scope, number, ...body });
}

async function getReview(url) {
  const resp = await fetch(url);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

// Who to ask for a review of the PR (Request review…'s dialog): {candidates: [{login, reason}]}.
export async function reviewCandidates(scope, number) {
  return getReview(`/api/reviews/candidates?scope=${encodeURIComponent(scope)}&number=${encodeURIComponent(number)}`);
}

// The merge methods the repo allows (Merge…'s dialog): {methods, default}.
export async function mergeOptions(scope) {
  return getReview(`/api/reviews/merge-options?scope=${encodeURIComponent(scope)}`);
}

// `body` is {command} or {text}, exactly one.
export async function reviewAction(scope, number, jobId, body) {
  return postReview('/api/reviews/action', { scope, number, job_id: jobId, ...body });
}

export async function reviewConfirm(id, yes) {
  return postReview('/api/reviews/confirm', { confirmation: id, yes });
}
