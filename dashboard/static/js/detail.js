// detail.js — ticket detail view

import { loadWorkspaces, loadEvents } from './api.js';
import { esc, timeAgo, fmtIso, stateBadgeHtml, PIPELINE_STAGES, STAGE_ORDER } from './helpers.js';
import { renderEventsHtml } from './events.js';
import { renderReportTabs, bindReportTabClicks } from './reports.js';
import { approveWorkspace, rejectWorkspace, retryWorkspace, takeControl, releaseControl, showConfirmDialog } from './actions.js';

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
    const actionBarHtml = stateVal === 'MANUAL_CONTROL'
      ? buildManualBanner(ws)
      : buildActionBar(ws, stateVal);
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
  const isBlocked = stateVal === 'BLOCKED' || stateVal === 'FAILED';
  const canTakeControl = !['DONE', 'ARCHIVED', 'MANUAL_CONTROL'].includes(stateVal);

  let buttons = '<span class="action-label">Actions</span>';
  if (isAwaiting) {
    buttons += `<button class="action-btn btn-approve" id="act-approve">Approve</button>`;
    buttons += `<button class="action-btn btn-reject" id="act-reject">Reject</button>`;
  }
  if (isBlocked) {
    buttons += `<button class="action-btn btn-retry" id="act-retry">Retry</button>`;
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

function buildPipeline(ws, stateVal) {
  const stageIdx = STAGE_ORDER[stateVal] != null ? STAGE_ORDER[stateVal] : -1;
  const prevIdx = ws.previous_state ? (STAGE_ORDER[ws.previous_state] ?? -1) : -1;

  let html = '<div class="pipeline-stages">';
  PIPELINE_STAGES.forEach((stage, idx) => {
    let dotClass = 'stage-dot';
    let labelClass = 'stage-label';
    let symbol = (idx + 1).toString();

    if (stateVal === 'MANUAL_CONTROL' && idx === prevIdx) {
      dotClass += ' current';
      labelClass += ' current';
      dotClass = dotClass.replace(' current', '');
      // Purple for manual control position
      symbol = '\u26A1';
      html += `<div class="pipeline-stage"><div class="stage-node">
        <div class="stage-dot" style="background:#2d1a3d;border-color:#8957e5;color:#d2a8ff;box-shadow:0 0 6px #8957e566;">${symbol}</div>
        <div class="stage-label" style="color:#d2a8ff;">${esc(stage)}</div>
      </div>`;
    } else if (stateVal === 'FAILED' && idx === stageIdx) {
      dotClass += ' failed'; labelClass += ' failed'; symbol = '!';
      html += `<div class="pipeline-stage"><div class="stage-node"><div class="${dotClass}">${symbol}</div><div class="${labelClass}">${esc(stage)}</div></div>`;
    } else if (stateVal === 'MANUAL_CONTROL' ? idx < prevIdx : idx < stageIdx) {
      dotClass += ' done'; labelClass += ' done'; symbol = '\u2713';
      html += `<div class="pipeline-stage"><div class="stage-node"><div class="${dotClass}">${symbol}</div><div class="${labelClass}">${esc(stage)}</div></div>`;
    } else if (idx === stageIdx && stateVal !== 'MANUAL_CONTROL') {
      dotClass += ' current'; labelClass += ' current';
      html += `<div class="pipeline-stage"><div class="stage-node"><div class="${dotClass}">${symbol}</div><div class="${labelClass}">${esc(stage)}</div></div>`;
    } else {
      html += `<div class="pipeline-stage"><div class="stage-node"><div class="${dotClass}">${symbol}</div><div class="${labelClass}">${esc(stage)}</div></div>`;
    }

    if (idx < PIPELINE_STAGES.length - 1) {
      const active = stateVal === 'MANUAL_CONTROL' ? prevIdx : stageIdx;
      const connDone = idx < active ? ' done' : '';
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

  let grid = `<div class="info-grid">
    <span class="info-label">Branch</span><span class="info-value">${esc(ws.branch || '\u2014')}</span>
    <span class="info-label">Repo</span><span class="info-value">${esc(ws.repo_id || '\u2014')}</span>
    <span class="info-label">Project</span><span class="info-value">${esc(ws.company_id || '\u2014')}</span>
    <span class="info-label">Started</span><span class="info-value">${esc(fmtIso(ws.started_at))}</span>
    <span class="info-label">Last updated</span><span class="info-value">${esc(fmtIso(ws.last_updated_at))}</span>`;
  if (ws.pr_url) {
    grid += `<span class="info-label">PR</span><span class="info-value"><a href="${esc(ws.pr_url)}" target="_blank">#${esc(String(ws.pr_number || ''))}</a></span>`;
  }
  if (iters) {
    grid += `<span class="info-label">Iterations</span><span class="info-value">${iters}</span>`;
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
