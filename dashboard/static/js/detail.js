// detail.js — ticket detail view

import { loadWorkspaces, loadEvents } from './api.js';
import { esc, timeAgo, fmtIso, stateBadgeHtml, PIPELINE_STAGES, STAGE_ORDER } from './helpers.js';
import { renderEventsHtml } from './events.js';
import { renderReportTabs, bindReportTabClicks } from './reports.js';
import { approveWorkspace, rejectWorkspace, retryWorkspace, takeControl, releaseControl, resumeWorkspace, archiveWorkspace, pauseWorkspace, unpauseWorkspace, showConfirmDialog } from './actions.js';

export async function renderDetail(ticketId, onBack) {
  const content = document.getElementById('content');
  content.innerHTML = '<div class="state-msg">Loading…</div>';

  try {
    const workspaces = await loadWorkspaces();
    const ws = workspaces.find(w => w.ticket_id === ticketId);

    if (!ws) {
      content.innerHTML = `<div class="state-msg" style="color:#f85149;">Workspace not found: ${esc(ticketId)}</div>`;
      return;
    }

    let events = [];
    try {
      events = await loadEvents({ ticketId, limit: 200 });
    } catch (e) { /* ignore */ }

    const stateVal = ws.current_state || 'NEW';

    // Build HTML sections
    const headerHtml = buildHeader(ws, stateVal, onBack);
    let actionBarHtml;
    if (stateVal === 'MANUAL_CONTROL') {
      actionBarHtml = buildManualBanner(ws);
    } else if (stateVal === 'DEFERRED') {
      actionBarHtml = buildDeferredBanner(ws) + buildActionBar(ws, stateVal);
    } else {
      actionBarHtml = buildActionBar(ws, stateVal);
    }
    const pipelineHtml = buildPipeline(ws, stateVal);
    const infoHtml = buildInfoSection(ws);
    const reportsHtml = renderReportTabs(ws.ticket_id, ws.reports, ws.meta);

    // Events — show last 5, expandable
    const recentEvents = events.slice(0, 5);
    const eventsPreview = renderEventsHtml(recentEvents, true);
    const allEventsHtml = renderEventsHtml(events, true);
    const expandLabel = events.length > 5 ? `<div style="cursor:pointer;color:#58a6ff;font-size:11px;" id="expand-events">Show all (${events.length} events)</div>` : '';

    content.innerHTML = `<div id="detail-view">
      ${headerHtml}
      ${actionBarHtml}
      <div class="pipeline-bar">
        <div class="pipeline-bar-title">Pipeline</div>
        ${pipelineHtml}
      </div>
      ${infoHtml}
      ${reportsHtml}
      <div class="detail-section">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
          <div class="detail-section-title" style="margin-bottom:0;">Event Timeline</div>
          ${expandLabel}
        </div>
        <div class="event-list" id="events-list">${eventsPreview}</div>
      </div>
    </div>`;

    // Bind expand events
    const expandBtn = document.getElementById('expand-events');
    if (expandBtn) {
      expandBtn.addEventListener('click', () => {
        document.getElementById('events-list').innerHTML = allEventsHtml;
        expandBtn.style.display = 'none';
      });
    }

    // Bind report tabs
    bindReportTabClicks();

    // Bind action buttons
    bindActionButtons(ticketId, ws, stateVal, onBack);

  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error: ${esc(String(e))}</div>`;
  }
}

function buildHeader(ws, stateVal, onBack) {
  return `<div class="detail-header">
    <button class="back-btn" id="back-btn">&larr; Back</button>
    <span class="detail-ticket-id">${esc(ws.ticket_id)}</span>
    ${stateBadgeHtml(stateVal)}
    <span class="detail-time-info">Started ${esc(timeAgo(ws.started_at))} &middot; Updated ${esc(timeAgo(ws.last_updated_at))}</span>
  </div>`;
}

function buildActionBar(ws, stateVal) {
  const isAwaiting = stateVal === 'AWAITING_APPROVAL';
  const isBlockedLike = stateVal === 'BLOCKED' || stateVal === 'FAILED';
  const isDeferred = stateVal === 'DEFERRED';
  const canArchive = ['FAILED', 'DONE', 'DEFERRED'].includes(stateVal);
  const canTakeControl = !['DONE', 'ARCHIVED', 'MANUAL_CONTROL'].includes(stateVal);

  let buttons = '<span class="action-label">Actions</span>';
  if (isAwaiting) {
    buttons += `<button class="action-btn btn-approve" id="act-approve">Approve</button>`;
    buttons += `<button class="action-btn btn-reject" id="act-reject">Reject</button>`;
  }
  if (isBlockedLike) {
    buttons += `<button class="action-btn btn-retry" id="act-retry">Retry</button>`;
  }
  if (isDeferred) {
    buttons += `<button class="action-btn btn-retry" id="act-resume">Resume now</button>`;
  }
  const PAUSEABLE_STATES = ['ANALYSIS', 'DEV', 'SCOPE_CHECK', 'QA', 'PUSHED', 'PR_REVIEW'];
  const canPause = PAUSEABLE_STATES.includes(stateVal);
  const isPaused = stateVal === 'PAUSED';
  if (canPause) {
    buttons += `<button class="action-btn btn-pause" id="act-pause">Pause</button>`;
  }
  if (isPaused) {
    buttons += `<button class="action-btn btn-pause" id="act-unpause">Unpause</button>`;
  }
  if (canArchive) {
    buttons += `<button class="action-btn btn-reject" id="act-archive">Archive</button>`;
  }
  if (canTakeControl) {
    buttons += `<span style="display:inline-block;width:1px;height:20px;background:#30363d;margin:0 4px;"></span>`;
    buttons += `<button class="action-btn btn-take-control" id="act-take-control">Take Control</button>`;
  }

  let links = '';
  if (ws.pr_url) {
    links += `<a href="${esc(ws.pr_url)}" target="_blank">PR #${esc(String(ws.pr_number || ''))}</a>`;
  }

  return `<div class="action-bar">
    ${buttons}
    <span class="action-links">${links}</span>
  </div>`;
}

function buildManualBanner(ws) {
  const since = timeAgo(ws.manual_control_started_at || ws.last_updated_at);
  const prev = ws.previous_state || '?';
  return `<div class="manual-banner">
    <div class="manual-banner-header">
      ${stateBadgeHtml('MANUAL_CONTROL')}
      <span style="font-size:12px;color:#d2a8ff;">You have control since ${esc(since)}</span>
      <span style="font-size:11px;color:#6e7681;">(was in ${esc(prev)})</span>
    </div>
    <div class="manual-banner-finish">
      <textarea class="manual-comment" id="manual-comment" placeholder="What did you do? (optional)"></textarea>
      <button class="action-btn btn-finished" id="act-finished">Finished</button>
    </div>
  </div>`;
}

function buildDeferredBanner(ws) {
  const retryAtIso = ws.retry_at;
  if (!retryAtIso) return '';
  const retryAt = new Date(retryAtIso);
  const now = new Date();
  const diffMs = retryAt.getTime() - now.getTime();
  const localLabel = retryAt.toLocaleString();
  let relative;
  if (diffMs <= 0) {
    relative = 'any moment';
  } else {
    const mins = Math.floor(diffMs / 60000);
    const hours = Math.floor(mins / 60);
    relative = hours > 0 ? `in ${hours}h ${mins % 60}m` : `in ${mins}m`;
  }
  const prev = ws.previous_state || '?';
  return `<div class="manual-banner">
    <div class="manual-banner-header">
      ${stateBadgeHtml('DEFERRED')}
      <span style="font-size:12px;color:#d2a8ff;">Waiting for Claude quota reset &middot; ${esc(localLabel)} (${esc(relative)})</span>
      <span style="font-size:11px;color:#6e7681;">(will resume from ${esc(prev)})</span>
    </div>
  </div>`;
}

function buildPipeline(ws, stateVal) {
  // For off-pipeline states (BLOCKED, FAILED, MANUAL_CONTROL, AWAITING_APPROVAL, DEFERRED),
  // fall back to previous_state so we still highlight where the ticket is "stuck".
  const OFF_PIPELINE = ['BLOCKED', 'FAILED', 'MANUAL_CONTROL', 'AWAITING_APPROVAL', 'DEFERRED'];
  const directIdx = STAGE_ORDER[stateVal];
  const prevIdx = ws.previous_state != null ? STAGE_ORDER[ws.previous_state] : undefined;
  const activeIdx = directIdx != null
    ? directIdx
    : (OFF_PIPELINE.includes(stateVal) && prevIdx != null ? prevIdx : -1);

  // What style to give the active stage
  let activeMode = 'current';
  if (stateVal === 'BLOCKED') activeMode = 'blocked';
  else if (stateVal === 'FAILED') activeMode = 'failed';
  else if (stateVal === 'MANUAL_CONTROL') activeMode = 'manual';
  else if (stateVal === 'AWAITING_APPROVAL') activeMode = 'awaiting';

  let html = '<div class="pipeline-stages">';
  PIPELINE_STAGES.forEach((stage, idx) => {
    let dotHtml;
    let labelHtml;

    if (idx === activeIdx) {
      if (activeMode === 'blocked') {
        dotHtml = `<div class="stage-dot" style="background:#3d1a1a;border-color:#da3633;color:#f85149;box-shadow:0 0 6px #da363366;">&#9888;</div>`;
        labelHtml = `<div class="stage-label" style="color:#f85149;">${esc(stage)}</div>`;
      } else if (activeMode === 'failed') {
        dotHtml = `<div class="stage-dot failed">!</div>`;
        labelHtml = `<div class="stage-label failed">${esc(stage)}</div>`;
      } else if (activeMode === 'manual') {
        dotHtml = `<div class="stage-dot" style="background:#2d1a3d;border-color:#8957e5;color:#d2a8ff;box-shadow:0 0 6px #8957e566;">&#9889;</div>`;
        labelHtml = `<div class="stage-label" style="color:#d2a8ff;">${esc(stage)}</div>`;
      } else if (activeMode === 'awaiting') {
        dotHtml = `<div class="stage-dot" style="background:#3d2f1a;border-color:#e3b341;color:#e3b341;box-shadow:0 0 6px #e3b34166;">?</div>`;
        labelHtml = `<div class="stage-label" style="color:#e3b341;">${esc(stage)}</div>`;
      } else {
        dotHtml = `<div class="stage-dot current">${idx + 1}</div>`;
        labelHtml = `<div class="stage-label current">${esc(stage)}</div>`;
      }
    } else if (idx < activeIdx) {
      dotHtml = `<div class="stage-dot done">&#10003;</div>`;
      labelHtml = `<div class="stage-label done">${esc(stage)}</div>`;
    } else {
      dotHtml = `<div class="stage-dot">${idx + 1}</div>`;
      labelHtml = `<div class="stage-label">${esc(stage)}</div>`;
    }

    html += `<div class="pipeline-stage"><div class="stage-node">${dotHtml}${labelHtml}</div>`;

    if (idx < PIPELINE_STAGES.length - 1) {
      const connDone = idx < activeIdx ? ' done' : '';
      html += `</div><div class="stage-connector${connDone}"></div>`;
    } else {
      html += '</div>';
    }
  });
  html += '</div>';
  return html;
}

function buildInfoSection(ws) {
  const iters = ws.stage_iterations
    ? Object.entries(ws.stage_iterations).map(([k, v]) => `${esc(k)}: ${esc(String(v))}`).join(', ')
    : '';

  const modelCell = ws.model_short
    ? `<span class="card-tag card-tag-model" title="${esc(ws.model || '')}">${esc(ws.model_short)}</span>`
    : '\u2014';
  let grid = `<div class="info-grid">
    <span class="info-label">Branch</span><span class="info-value">${esc(ws.branch || '\u2014')}</span>
    <span class="info-label">Repo</span><span class="info-value">${esc(ws.repo_id || '\u2014')}</span>
    <span class="info-label">Project</span><span class="info-value">${esc(ws.company_id || '\u2014')}</span>
    <span class="info-label">Model</span><span class="info-value">${modelCell}</span>
    <span class="info-label">Started</span><span class="info-value">${esc(fmtIso(ws.started_at))}</span>
    <span class="info-label">Last updated</span><span class="info-value">${esc(fmtIso(ws.last_updated_at))}</span>`;
  if (iters) {
    grid += `<span class="info-label">Iterations</span><span class="info-value">${iters}</span>`;
  }
  if (Array.isArray(ws.links) && ws.links.length > 0) {
    const linksHtml = ws.links.map(link =>
      `<a href="${esc(link.url)}" target="_blank" rel="noopener" class="info-link info-link-${esc(link.type || 'other')}">${esc(link.label)}</a>`
    ).join('');
    grid += `<span class="info-label">Links</span><span class="info-value info-links">${linksHtml}</span>`;
  }
  grid += '</div>';

  let errorPanel = '';
  if (ws.error) {
    errorPanel = `<div style="background:#161b22;border:1px solid #da3633;border-radius:8px;padding:14px 16px;width:320px;">
      <div style="font-size:10px;font-weight:600;text-transform:uppercase;color:#f85149;letter-spacing:.06em;margin-bottom:10px;">Error / Escalation</div>
      <div style="font-size:12px;color:#f85149;line-height:1.5;">${esc(ws.error)}</div>
    </div>`;
  }

  return `<div style="display:flex;gap:14px;margin-bottom:14px;">
    <div class="detail-section" style="flex:1;min-width:0;margin-bottom:0;">
      <div class="detail-section-title">Info</div>${grid}
    </div>
    ${errorPanel}
  </div>`;
}

function bindActionButtons(ticketId, ws, stateVal, onBack) {
  // Back
  const backBtn = document.getElementById('back-btn');
  if (backBtn) backBtn.addEventListener('click', () => onBack(ws.company_id));

  // Approve
  const approveBtn = document.getElementById('act-approve');
  if (approveBtn) {
    approveBtn.addEventListener('click', async () => {
      try {
        await approveWorkspace(ticketId);
        await renderDetail(ticketId, onBack);
      } catch (e) { alert('Approve failed: ' + e.message); }
    });
  }

  // Reject
  const rejectBtn = document.getElementById('act-reject');
  if (rejectBtn) {
    rejectBtn.addEventListener('click', async () => {
      try {
        await rejectWorkspace(ticketId);
        await renderDetail(ticketId, onBack);
      } catch (e) { alert('Reject failed: ' + e.message); }
    });
  }

  // Retry
  const retryBtn = document.getElementById('act-retry');
  if (retryBtn) {
    retryBtn.addEventListener('click', async () => {
      try {
        await retryWorkspace(ticketId);
        await renderDetail(ticketId, onBack);
      } catch (e) { alert('Retry failed: ' + e.message); }
    });
  }

  // Resume (from DEFERRED)
  const resumeBtn = document.getElementById('act-resume');
  if (resumeBtn) {
    resumeBtn.addEventListener('click', async () => {
      try {
        await resumeWorkspace(ticketId);
        location.reload();
      } catch (e) { alert('Resume failed: ' + e.message); }
    });
  }

  // Archive (from FAILED / DONE / DEFERRED)
  const archiveBtn = document.getElementById('act-archive');
  if (archiveBtn) {
    archiveBtn.addEventListener('click', async () => {
      showConfirmDialog(
        `Archive ${ticketId}?`,
        '<p>This workspace will be hidden from the board. The source directory will be cleaned up on the next cleanup sweep.</p>',
        'Archive',
        async () => {
          try {
            await archiveWorkspace(ticketId);
            location.href = '/';
          } catch (e) { alert('Archive failed: ' + e.message); }
        },
      );
    });
  }

  // Take Control
  const tcBtn = document.getElementById('act-take-control');
  if (tcBtn) {
    tcBtn.addEventListener('click', async () => {
      try {
        const result = await takeControl(ticketId, false);
        if (result.status === 'agent_running') {
          showConfirmDialog(
            `Take Control of ${ticketId}?`,
            `<div style="background:#3d1a1a22;border:1px solid #da363366;border-radius:6px;padding:10px 12px;margin-bottom:8px;">
              <div style="font-size:12px;color:#f85149;font-weight:600;">Agent is currently running</div>
              <div style="font-size:11px;color:#c9d1d9;">${esc(result.agent)} &mdash; started ${esc(result.started_ago)} ago</div>
              <div style="font-size:11px;color:#8b949e;margin-top:4px;">Taking control will stop this agent.</div>
            </div>`,
            'Stop Agent & Take Control',
            async () => {
              await takeControl(ticketId, true);
              await renderDetail(ticketId, onBack);
            }
          );
        } else {
          await renderDetail(ticketId, onBack);
        }
      } catch (e) { alert('Take control failed: ' + e.message); }
    });
  }

  // Pause
  const pauseBtn = document.getElementById('act-pause');
  console.log('[detail-pause] button found:', !!pauseBtn, 'state:', stateVal, 'ticket:', ticketId);
  if (pauseBtn) {
    pauseBtn.addEventListener('click', async () => {
      console.log('[detail-pause] click on ticket', ticketId);
      try {
        const result = await pauseWorkspace(ticketId, false);
        console.log('[detail-pause] response', result);
        if (result && result.status === 'agent_running') {
          showConfirmDialog(
            `Pause ${ticketId}?`,
            `<div style="background:#3d1a1a22;border:1px solid #da363366;border-radius:6px;padding:10px 12px;margin-bottom:8px;">
              <div style="font-size:12px;color:#f85149;font-weight:600;">Agent is currently running</div>
              <div style="font-size:11px;color:#c9d1d9;">${esc(result.agent)} &mdash; started ${esc(result.started_ago)} ago</div>
              <div style="font-size:11px;color:#8b949e;margin-top:4px;">Pausing will stop this agent.</div>
            </div>`,
            'Stop Agent & Pause',
            async () => {
              await pauseWorkspace(ticketId, true);
              await renderDetail(ticketId, onBack);
            }
          );
        } else {
          await renderDetail(ticketId, onBack);
        }
      } catch (e) { alert('Pause failed: ' + e.message); }
    });
  }

  // Unpause
  const unpauseBtn = document.getElementById('act-unpause');
  console.log('[detail-unpause] button found:', !!unpauseBtn, 'state:', stateVal, 'ticket:', ticketId);
  if (unpauseBtn) {
    unpauseBtn.addEventListener('click', async () => {
      console.log('[detail-unpause] click on ticket', ticketId);
      try {
        const result = await unpauseWorkspace(ticketId);
        console.log('[detail-unpause] response', result);
        await renderDetail(ticketId, onBack);
      } catch (e) {
        console.error('[detail-unpause] failed', e);
        alert('Unpause failed: ' + e.message);
      }
    });
  }

  // Finished (release control)
  const finBtn = document.getElementById('act-finished');
  if (finBtn) {
    finBtn.addEventListener('click', async () => {
      const comment = document.getElementById('manual-comment')?.value || '';
      try {
        await releaseControl(ticketId, comment);
        await renderDetail(ticketId, onBack);
      } catch (e) { alert('Release failed: ' + e.message); }
    });
  }
}
