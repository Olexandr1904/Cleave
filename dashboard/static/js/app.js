// app.js — state management, routing, sidebar, auto-refresh

import { loadWorkspaces, loadEvents, loadDaemonStatus } from './api.js';
import { esc, stateBadgeHtml } from './helpers.js';
import { renderBoard } from './board.js';
import { renderDetail } from './detail.js';
import { renderEventsHtml } from './events.js';
import { setMode, showConfirmDialog } from './actions.js';

const state = {
  view: 'board',
  projectId: null,
  ticketId: null,
  filterType: '',
  timer: null,
  showDone: true,
};

// ── Navigation ──
function setHash(h) {
  state._suppressHashChange = true;
  if (location.hash !== h) location.hash = h;
  setTimeout(() => { state._suppressHashChange = false; }, 0);
}

function showBoard(projectId, fromHash) {
  state.view = 'board';
  state.projectId = projectId;
  state.ticketId = null;
  document.getElementById('view-title').textContent = projectId ? `Board: ${projectId}` : 'Board';
  document.getElementById('toolbar-eventlog-controls').style.display = 'none';
  updateActiveNav(projectId ? `nav-proj-${projectId}` : 'nav-board');
  scheduleAutoRefresh();
  doRenderBoard();
  if (!fromHash) setHash(projectId ? `#/board/${projectId}` : '#/board');
}

function showEventLog(fromHash) {
  state.view = 'eventlog';
  state.ticketId = null;
  document.getElementById('view-title').textContent = 'Event Log';
  document.getElementById('toolbar-eventlog-controls').style.display = 'flex';
  updateActiveNav('nav-eventlog');
  scheduleAutoRefresh();
  doRenderEventLog();
  if (!fromHash) setHash('#/eventlog');
}

function showDetail(ticketId, fromHash) {
  state.view = 'detail';
  state.ticketId = ticketId;
  document.getElementById('toolbar-eventlog-controls').style.display = 'none';
  document.getElementById('view-title').textContent = `Ticket: ${ticketId}`;
  stopAutoRefresh();
  renderDetail(ticketId, (projectId) => showBoard(projectId));
  if (!fromHash) setHash(`#/ticket/${ticketId}`);
}

function routeFromHash() {
  const h = location.hash || '#/board';
  const m = h.match(/^#\/(board|eventlog|ticket)(?:\/(.+))?$/);
  if (!m) { showBoard(null, true); return; }
  const [, view, arg] = m;
  if (view === 'board') showBoard(arg || null, true);
  else if (view === 'eventlog') showEventLog(true);
  else if (view === 'ticket' && arg) showDetail(arg, true);
  else showBoard(null, true);
}

function updateActiveNav(id) {
  document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
}

// ── Board ──
async function doRenderBoard() {
  const { workspaces } = await renderBoard(state.projectId, state.showDone);
  // Update sidebar project list from workspace data
  updateProjectSidebar(workspaces || []);
  // Update toolbar stats
  updateToolbarStats(workspaces || []);
}

// ── Event Log ──
async function doRenderEventLog() {
  const content = document.getElementById('content');
  try {
    let events = await loadEvents({ projectId: state.projectId });
    if (state.filterType) {
      events = events.filter(e => e.event_type === state.filterType);
    }
    content.innerHTML = `<div class="event-list">${renderEventsHtml(events, false)}</div>`;
  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error: ${esc(String(e))}</div>`;
  }
}

// ── Sidebar project list (from workspace data, not events) ──
function updateProjectSidebar(workspaces) {
  const pl = document.getElementById('project-list');
  const projects = [...new Set(workspaces.map(ws => ws.company_id).filter(Boolean))];
  if (projects.length === 0) {
    pl.innerHTML = '<div style="padding:6px 16px;color:#6e7681;font-size:12px;">No projects yet.</div>';
    return;
  }
  pl.innerHTML = projects.map(p =>
    `<a class="nav-link" id="nav-proj-${esc(p)}">${esc(p)}</a>`
  ).join('');
  // Bind clicks
  projects.forEach(p => {
    const el = document.getElementById(`nav-proj-${p}`);
    if (el) el.addEventListener('click', () => showBoard(p));
  });
}

// ── Toolbar stats ──
function updateToolbarStats(workspaces) {
  const stats = document.getElementById('toolbar-stats');
  if (!stats) return;
  const active = workspaces.filter(ws => !['DONE', 'FAILED', 'ARCHIVED'].includes(ws.current_state)).length;
  const blocked = workspaces.filter(ws => ws.current_state === 'BLOCKED').length;
  const awaiting = workspaces.filter(ws => ws.current_state === 'AWAITING_APPROVAL').length;
  const manual = workspaces.filter(ws => ws.current_state === 'MANUAL_CONTROL').length;

  let parts = [`${active} active`];
  if (blocked) parts.push(`<span class="stat-blocked">${blocked} blocked</span>`);
  if (awaiting) parts.push(`<span class="stat-awaiting">${awaiting} awaiting</span>`);
  if (manual) parts.push(`<span style="color:#d2a8ff;">${manual} manual</span>`);
  stats.innerHTML = parts.join(' &middot; ');
}

// ── Daemon status in sidebar ──
async function updateDaemonStatus() {
  try {
    const data = await loadDaemonStatus();
    const el = document.getElementById('daemon-status');
    if (el) {
      el.innerHTML = `
        <div class="daemon-status"><span class="status-dot online"></span>Mode: <span style="color:#e3b341;">${esc(data.mode)}</span></div>
        <div class="daemon-status" style="color:#6e7681;">Active: ${data.active} &middot; Blocked: ${data.blocked}</div>`;
    }
    renderModeIndicator(data.mode);
  } catch (e) {
    const el = document.getElementById('daemon-status');
    if (el) el.innerHTML = '<div class="daemon-status"><span class="status-dot offline"></span>Offline</div>';
    renderModeIndicator(null);
  }
}

// ── Mode indicator in toolbar ──
function renderModeIndicator(mode) {
  const container = document.getElementById('mode-indicator');
  if (!container) return;

  if (!mode) {
    container.innerHTML = '<span class="mode-pill mode-offline">Offline</span>';
    return;
  }

  const isAuto = mode === 'auto';
  const pillClass = isAuto ? 'mode-auto' : 'mode-manual';
  const label = isAuto ? 'Auto' : 'Manual';
  const targetMode = isAuto ? 'manual' : 'auto';
  const targetLabel = isAuto ? 'Manual' : 'Auto';

  container.innerHTML = `
    <span class="mode-pill ${pillClass}" id="mode-toggle" title="Click to switch mode">
      <span class="mode-dot"></span>
      ${esc(label)}
    </span>
    <span class="mode-hint" id="mode-hint">
      <span class="mode-hint-icon">?</span>
      <span class="mode-hint-tooltip">
        <strong>Auto mode</strong><br>
        Jira is polled for new tickets. Approval gates are skipped — tickets flow through stages automatically.<br><br>
        <strong>Manual mode</strong><br>
        Jira polling is paused. Tickets stop at approval gates (Analysis, QA, PR Review) and wait for your approval in the dashboard.
      </span>
    </span>`;

  container.querySelector('#mode-toggle').addEventListener('click', () => {
    const warn = isAuto
      ? 'Switching to <strong>Manual</strong> will pause Jira polling and enable approval gates. Tickets will wait for your approval at each stage.'
      : 'Switching to <strong>Auto</strong> will resume Jira polling and skip approval gates. Pending approvals will be auto-approved.';

    showConfirmDialog(
      `Switch to ${targetLabel} mode?`,
      `<div style="font-size:12px;color:#c9d1d9;line-height:1.5;">${warn}</div>`,
      `Switch to ${targetLabel}`,
      async () => {
        try {
          await setMode(targetMode);
          await updateDaemonStatus();
        } catch (err) {
          alert('Failed to change mode: ' + err.message);
        }
      }
    );
  });
}

// ── Auto-refresh ──
function stopAutoRefresh() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

function scheduleAutoRefresh() {
  stopAutoRefresh();
  const cb = document.getElementById('auto-refresh-cb');
  if (cb && cb.checked) {
    state.timer = setInterval(() => {
      if (state.view === 'board') doRenderBoard();
      else if (state.view === 'eventlog') doRenderEventLog();
      updateDaemonStatus();
    }, 30000);
  }
}

// ── Init ──
async function init() {
  // Bind nav
  document.getElementById('nav-board').addEventListener('click', () => showBoard(null));
  document.getElementById('nav-eventlog').addEventListener('click', () => showEventLog());

  // Bind new project button
  document.getElementById('new-project-btn')?.addEventListener('click', () => {
    import('./project-wizard.js').then(({ openWizard }) => openWizard());
  });

  // Delegated card click — survives innerHTML replacement on auto-refresh
  document.getElementById('content').addEventListener('click', (e) => {
    if (state.view !== 'board') return;
    const card = e.target.closest('.card[data-ticket]');
    if (!card) return;
    if (e.target.closest('[data-action]')) return;
    showDetail(card.dataset.ticket);
  });

  // Bind filter
  document.getElementById('filter-type').addEventListener('change', () => {
    state.filterType = document.getElementById('filter-type').value;
    doRenderEventLog();
  });

  // Bind refresh
  document.getElementById('toolbar-refresh-btn').addEventListener('click', () => {
    if (state.view === 'board') doRenderBoard();
    else if (state.view === 'eventlog') doRenderEventLog();
    else if (state.view === 'detail') renderDetail(state.ticketId, (pid) => showBoard(pid));
  });

  // Bind auto-refresh toggle
  document.getElementById('auto-refresh-cb').addEventListener('change', () => {
    if (state.view !== 'detail') scheduleAutoRefresh();
  });

  // Bind hide-done toggle
  const hideDone = document.getElementById('toggle-done');
  if (hideDone) {
    hideDone.addEventListener('change', () => {
      state.showDone = !hideDone.checked;
      doRenderBoard();
    });
  }

  // Hash routing — survives page refresh
  window.addEventListener('hashchange', () => {
    if (state._suppressHashChange) return;
    routeFromHash();
  });

  // Initial load — honor URL hash if present, else default board
  if (location.hash && location.hash.startsWith('#/')) {
    routeFromHash();
  } else {
    updateActiveNav('nav-board');
    await doRenderBoard();
  }
  await updateDaemonStatus();
  scheduleAutoRefresh();
}

init();
