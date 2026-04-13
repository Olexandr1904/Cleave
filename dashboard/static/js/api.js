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
