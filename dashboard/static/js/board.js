// board.js — board view rendering

import { loadWorkspaces, loadHealth } from './api.js';
import { esc, timeAgo, stateBadgeHtml } from './helpers.js';
import { approveWorkspace, pauseWorkspace, unpauseWorkspace, retryWorkspace } from './actions.js';

export async function renderBoard(projectId, showDone = true) {
  const content = document.getElementById('content');
  try {
    const workspaces = await loadWorkspaces(projectId);

    if (workspaces.length === 0) {
      content.innerHTML = `<div id="health-strip-container"></div>
        <div class="empty-state">
          <div class="empty-state-icon">📋</div>
          <h2>No projects yet</h2>
          <p>Add your first project to start the autonomous pipeline.</p>
          <button id="empty-add-project" class="btn-primary">+ New Project</button>
        </div>`;
      renderHealthStrip(document.getElementById('health-strip-container'));
      document.getElementById('empty-add-project').onclick = () => {
        import('./project-wizard.js').then(({ openWizard }) => openWizard());
      };
      return { workspaces };
    }

    let filtered = workspaces;
    if (!showDone) {
      filtered = workspaces.filter(ws => !['DONE', 'ARCHIVED', 'SETUP_DONE'].includes(ws.current_state));
    }

    // Sort: BLOCKED first, AWAITING second, DEFERRED third, active by stage, DONE/ARCHIVED last
    const stateOrder = {
      BLOCKED: 0, AWAITING_APPROVAL: 1, DEFERRED: 2, MANUAL_CONTROL: 3, PAUSED: 4,
      DEV: 5, ANALYSIS: 6, SCOPE_CHECK: 7, QA: 8, PR_REVIEW: 9, PUSHED: 10,
      NEW: 11, FAILED: 12, DONE: 13, SETUP_DONE: 13, ARCHIVED: 14,
    };
    filtered.sort((a, b) => (stateOrder[a.current_state] ?? 99) - (stateOrder[b.current_state] ?? 99));

    // Group by project
    const byProject = {};
    filtered.forEach(ws => {
      const proj = ws.company_id || 'unknown';
      if (!byProject[proj]) byProject[proj] = [];
      byProject[proj].push(ws);
    });

    let html = '';
    for (const [proj, wsList] of Object.entries(byProject)) {
      html += `<div class="project-group">
        <div class="project-group-title">${esc(proj)}</div>
        <div class="cards-grid">`;
      for (const ws of wsList) {
        html += renderCard(ws);
      }
      html += `</div></div>`;
    }

    content.innerHTML = `<div id="health-strip-container"></div>` + html;
    renderHealthStrip(document.getElementById('health-strip-container'));

    // Bind inline approve buttons
    content.querySelectorAll('[data-action="approve"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tid = btn.dataset.ticket;
        try {
          await approveWorkspace(tid);
          await renderBoard(projectId, showDone);
        } catch (err) {
          alert('Approve failed: ' + err.message);
        }
      });
    });

    // Bind inline pause buttons
    const pauseBtns = content.querySelectorAll('[data-action="pause"]');
    console.log('[pause] binding', pauseBtns.length, 'pause buttons');
    pauseBtns.forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tid = btn.dataset.ticket;
        console.log('[pause] click on ticket', tid);
        try {
          const result = await pauseWorkspace(tid, false);
          console.log('[pause] response', result);
          if (result && result.status === 'agent_running') {
            const ok = confirm(`Agent ${result.agent} is currently running for ${tid} (started ${result.started_ago} ago). Pausing will stop the agent. Continue?`);
            if (!ok) return;
            const r2 = await pauseWorkspace(tid, true);
            console.log('[pause] confirm response', r2);
          }
          await renderBoard(projectId, showDone);
        } catch (err) {
          console.error('[pause] failed', err);
          alert('Pause failed: ' + err.message);
        }
      });
    });

    // Bind inline unpause buttons
    const unpauseBtns = content.querySelectorAll('[data-action="unpause"]');
    console.log('[unpause] binding', unpauseBtns.length, 'unpause buttons');
    unpauseBtns.forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tid = btn.dataset.ticket;
        console.log('[unpause] click on ticket', tid);
        try {
          const result = await unpauseWorkspace(tid);
          console.log('[unpause] response', result);
          await renderBoard(projectId, showDone);
        } catch (err) {
          console.error('[unpause] failed', err);
          alert('Unpause failed: ' + err.message);
        }
      });
    });

    // Bind inline retry buttons
    content.querySelectorAll('[data-action="retry"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tid = btn.dataset.ticket;
        try {
          await retryWorkspace(tid);
          await renderBoard(projectId, showDone);
        } catch (err) {
          alert('Retry failed: ' + err.message);
        }
      });
    });

    // Bind inline delete buttons
    content.querySelectorAll('[data-action="delete"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tid = btn.dataset.ticket;
        if (!confirm(`Delete workspace for ${tid}? This removes all local data. The ticket will be re-picked up on next poll.`)) return;
        try {
          const resp = await fetch(`/api/workspaces/${encodeURIComponent(tid)}/delete`, { method: 'POST' });
          if (!resp.ok) { const d = await resp.json(); throw new Error(d.message || resp.statusText); }
          await renderBoard(projectId, showDone);
        } catch (err) {
          alert('Delete failed: ' + err.message);
        }
      });
    });

    // Bind inline clean-source buttons (DONE cards only)
    content.querySelectorAll('[data-action="clean"]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const tid = btn.dataset.ticket;
        if (!confirm(`Remove source code for ${tid}? Reports and metadata are kept. Frees disk space.`)) return;
        btn.disabled = true;
        btn.textContent = '...';
        try {
          const resp = await fetch(`/api/workspaces/${encodeURIComponent(tid)}/clean`, { method: 'POST' });
          if (!resp.ok) { const d = await resp.json(); throw new Error(d.message || resp.statusText); }
          await renderBoard(projectId, showDone);
        } catch (err) {
          alert('Clean failed: ' + err.message);
          btn.disabled = false;
          btn.textContent = 'Clean';
        }
      });
    });

    return { workspaces };
  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error loading workspaces: ${esc(String(e))}</div>`;
    return { workspaces: [] };
  }
}

async function renderHealthStrip(container) {
  if (!container) return;
  let data;
  try {
    data = await loadHealth();
  } catch (e) {
    container.innerHTML = '';
    return;
  }

  const projects = data.projects || [];
  if (projects.length === 0) {
    container.innerHTML = '';
    return;
  }

  const unhealthy = projects.filter(p => p.status !== 'green');
  if (unhealthy.length === 0) {
    container.innerHTML = '';
    return;
  }

  const rowsHtml = unhealthy.map(p => {
    const badChecks = p.checks.filter(c => !c.ok);
    const checksHtml = badChecks.map(c => `
      <div class="health-check">
        <span class="health-check-name">${esc(c.name)}</span>
        <span class="health-check-target">${esc(c.target)}</span>
        <span class="health-check-reason">${esc(c.reason)}</span>
        ${c.fix_hint ? `<code class="health-check-fix">${esc(c.fix_hint)}</code>` : ''}
      </div>`).join('');
    return `
      <div class="health-row">
        <span class="health-dot ${esc(p.status)}"></span>
        <span class="health-project">${esc(p.project_id)}</span>
        <div class="health-checks">${checksHtml}</div>
      </div>`;
  }).join('');

  const topStatus = unhealthy.some(p => p.status === 'red') ? 'red' : 'yellow';
  container.innerHTML = `
    <div class="health-strip ${topStatus}">
      <div class="health-strip-header">
        <span class="health-dot ${topStatus}"></span>
        <span>${unhealthy.length} project(s) need attention</span>
        <button class="health-refresh" id="health-refresh">&#x21bb;</button>
      </div>
      ${rowsHtml}
    </div>`;
  bindHealthRefresh(container);
}

function bindHealthRefresh(container) {
  const btn = container.querySelector('#health-refresh');
  if (btn) {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await loadHealth(true);
      } finally {
        btn.disabled = false;
        renderHealthStrip(container);
      }
    });
  }
}

function renderCard(ws) {
  const stateVal = ws.current_state || 'NEW';
  const dimmed = ['DONE', 'ARCHIVED', 'SETUP_DONE'].includes(stateVal);

  let cardClass = 'card';
  if (stateVal === 'AWAITING_APPROVAL') cardClass += ' card-awaiting';
  if (stateVal === 'MANUAL_CONTROL') cardClass += ' card-manual';
  if (stateVal === 'PAUSED') cardClass += ' card-paused';
  if (dimmed) cardClass += ' card-dimmed';

  // Line 1: ID + state badge + pause/resume + retry + delete
  const PAUSEABLE_STATES = ['ANALYSIS', 'DEV', 'SCOPE_CHECK', 'QA', 'PUSHED', 'PR_REVIEW'];
  let pauseIcon = '';
  if (PAUSEABLE_STATES.includes(stateVal)) {
    pauseIcon = `<button class="card-icon-btn" data-action="pause" data-ticket="${esc(ws.ticket_id)}" title="Pause" onclick="event.stopPropagation()">⏸</button>`;
  } else if (stateVal === 'PAUSED') {
    pauseIcon = `<button class="card-icon-btn" data-action="unpause" data-ticket="${esc(ws.ticket_id)}" title="Resume" onclick="event.stopPropagation()">▶</button>`;
  }
  let retryIcon = '';
  if (stateVal === 'FAILED') {
    retryIcon = `<button class="card-icon-btn" data-action="retry" data-ticket="${esc(ws.ticket_id)}" title="Retry" onclick="event.stopPropagation()">↻</button>`;
  }
  const deleteIcon = `<button class="card-icon-btn card-icon-delete" data-action="delete" data-ticket="${esc(ws.ticket_id)}" title="Delete" onclick="event.stopPropagation()">✕</button>`;

  // Line 3: error > manual-control note > omitted
  let noteHtml = '';
  if (ws.error) {
    noteHtml = `<div class="card-error" title="${esc(ws.error)}">${esc(ws.error)}</div>`;
  } else if (stateVal === 'MANUAL_CONTROL') {
    noteHtml = `<div class="card-manual-note">You have control</div>`;
  }

  // Line 4: meta — repo · time · iter
  const iters = ws.stage_iterations || {};
  const totalIters = Object.values(iters).reduce((a, b) => a + b, 0);
  const metaParts = [];
  if (ws.repo_id) metaParts.push(esc(ws.repo_id));
  metaParts.push(esc(timeAgo(ws.started_at)));
  if (totalIters > 0) metaParts.push(`iter ${totalIters}`);
  const metaHtml = `<div class="card-meta">${metaParts.join(' <span class="card-meta-sep">·</span> ')}</div>`;

  // Footer: PR + Jira tags + contextual button (Approve/Clean)
  const prTag = ws.pr_url
    ? `<a class="card-tag card-tag-pr" href="${esc(ws.pr_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">PR #${esc(String(ws.pr_number || ''))}</a>`
    : '';
  const jiraLink = (ws.links || []).find(l => l && l.type === 'jira');
  const jiraTag = jiraLink
    ? `<a class="card-tag card-tag-jira" href="${esc(jiraLink.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Jira</a>`
    : '';
  let contextualBtn = '';
  if (stateVal === 'AWAITING_APPROVAL') {
    contextualBtn = `<button class="action-btn btn-approve" data-action="approve" data-ticket="${esc(ws.ticket_id)}">Approve</button>`;
  } else if (dimmed && ws.workspace_root) {
    contextualBtn = `<button class="action-btn btn-clean" data-action="clean" data-ticket="${esc(ws.ticket_id)}" onclick="event.stopPropagation()" title="Remove source code to free disk space">Clean</button>`;
  }
  const footerHtml = (prTag || jiraTag || contextualBtn)
    ? `<div class="card-footer">
         ${prTag}
         ${jiraTag}
         <span class="card-footer-spacer"></span>
         ${contextualBtn}
       </div>`
    : '';

  return `<div class="${cardClass}" data-ticket="${esc(ws.ticket_id)}">
    <div class="card-line1">
      <span class="card-id">${esc(ws.ticket_id)}</span>
      ${stateBadgeHtml(stateVal)}
      <span class="card-line1-spacer"></span>
      ${pauseIcon}
      ${retryIcon}
      ${deleteIcon}
    </div>
    <div class="card-line2">
      <div class="card-title" title="${esc(ws.title || '')}">${esc(ws.title || '')}</div>
    </div>
    ${noteHtml}
    ${metaHtml}
    ${footerHtml}
  </div>`;
}
