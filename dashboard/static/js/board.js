// board.js — board view rendering

import { loadWorkspaces } from './api.js';
import { esc, timeAgo, stateBadgeHtml } from './helpers.js';
import { approveWorkspace } from './actions.js';

export async function renderBoard(projectId, showDone = true) {
  const content = document.getElementById('content');
  try {
    const workspaces = await loadWorkspaces(projectId);

    if (workspaces.length === 0) {
      content.innerHTML = '<div class="state-msg">No workspaces found.</div>';
      return { workspaces };
    }

    let filtered = workspaces;
    if (!showDone) {
      filtered = workspaces.filter(ws => !['DONE', 'FAILED', 'ARCHIVED'].includes(ws.current_state));
    }

    // Sort: BLOCKED first, AWAITING second, DEFERRED third, active by stage, DONE/ARCHIVED last
    const stateOrder = {
      BLOCKED: 0, AWAITING_APPROVAL: 1, DEFERRED: 2, MANUAL_CONTROL: 3,
      DEV: 4, ANALYSIS: 5, SCOPE_CHECK: 6, QA: 7, PR_REVIEW: 8, PUSHED: 9,
      NEW: 10, DONE: 11, FAILED: 12, ARCHIVED: 13,
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

    content.innerHTML = html;

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

    return { workspaces };
  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error loading workspaces: ${esc(String(e))}</div>`;
    return { workspaces: [] };
  }
}

function renderCard(ws) {
  const stateVal = ws.current_state || 'NEW';
  let cardClass = 'card';
  if (stateVal === 'BLOCKED') cardClass += ' card-blocked';
  if (stateVal === 'AWAITING_APPROVAL') cardClass += ' card-awaiting';
  if (stateVal === 'MANUAL_CONTROL') cardClass += ' card-manual';

  const dimmed = ['DONE', 'FAILED', 'ARCHIVED'].includes(stateVal);

  const prLink = ws.pr_url
    ? `<a class="card-pr-link" href="${esc(ws.pr_url)}" target="_blank" onclick="event.stopPropagation()">PR #${esc(String(ws.pr_number || ''))}</a>`
    : '';

  const errorHtml = ws.error
    ? `<div class="card-error" title="${esc(ws.error)}">${esc(ws.error)}</div>`
    : '';

  const approveBtn = stateVal === 'AWAITING_APPROVAL'
    ? `<button class="action-btn btn-approve" data-action="approve" data-ticket="${esc(ws.ticket_id)}" style="padding:1px 8px;font-size:10px;">Approve</button>`
    : '';

  const manualLabel = stateVal === 'MANUAL_CONTROL'
    ? `<div style="font-size:10px;color:#d2a8ff;">You have control</div>`
    : '';

  // Iteration info
  const iters = ws.stage_iterations || {};
  const totalIters = Object.values(iters).reduce((a, b) => a + b, 0);
  const iterLabel = totalIters > 0 ? `<span style="font-size:10px;color:#58a6ff;">iter ${totalIters}</span>` : '';

  return `<div class="${cardClass}" data-ticket="${esc(ws.ticket_id)}" style="${dimmed ? 'opacity:0.5;' : ''}">
    <div class="card-header">
      <span class="card-ticket">${esc(ws.ticket_id)}</span>
      ${stateBadgeHtml(stateVal)}
    </div>
    <div class="card-repo">${esc(ws.repo_id || '')}</div>
    ${errorHtml}
    ${manualLabel}
    <div class="card-footer">
      <span class="card-time">${esc(timeAgo(ws.started_at))}</span>
      ${iterLabel}
      ${approveBtn}
      ${prLink}
    </div>
  </div>`;
}
