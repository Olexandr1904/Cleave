// helpers.js — utility functions shared across modules

export function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function fmtTs(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (isNaN(d)) return esc(ts);
  return d.toLocaleString('en-GB', {
    day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).replace(',', '');
}

export function timeAgo(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  if (isNaN(d)) return '';
  const diffMs = Date.now() - d.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

export function fmtIso(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  if (isNaN(d)) return esc(isoStr);
  return d.toLocaleString('en-GB', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).replace(',', '');
}

export const PIPELINE_STAGES = ['NEW', 'ANALYSIS', 'DEV', 'SCOPE_CHECK', 'QA', 'PUSHED', 'PR_REVIEW', 'DONE'];
export const STAGE_ORDER = {};
PIPELINE_STAGES.forEach((s, i) => { STAGE_ORDER[s] = i; });

export const BADGE_CLASS = {
  agent_dispatched: 'badge-green', agent_completed: 'badge-green',
  workspace_created: 'badge-green', pr_created: 'badge-green',
  project_loaded: 'badge-green', workspace_resumed: 'badge-green',
  agent_failed: 'badge-red', escalation_sent: 'badge-red',
  stage_transition: 'badge-blue',
  tg_message_received: 'badge-yellow', tg_message_sent: 'badge-yellow', intent_parsed: 'badge-yellow',
  approval_requested: 'badge-purple', poll_cycle: 'badge-gray', daemon_started: 'badge-gray',
  dashboard_approve: 'badge-green', dashboard_reject: 'badge-red',
  dashboard_retry: 'badge-blue', manual_control_started: 'badge-purple',
  manual_control_released: 'badge-purple', mode_changed: 'badge-purple',
};

export function badgeClass(type) {
  return BADGE_CLASS[type] || 'badge-gray';
}

export function stateBadgeHtml(stateVal) {
  const cls = 'state-' + (stateVal || 'NEW').replace(/[^A-Z_]/g, '');
  let pulseClass = '';
  if (stateVal === 'BLOCKED') pulseClass = ' badge-pulse-red';
  if (stateVal === 'AWAITING_APPROVAL') pulseClass = ' badge-pulse-yellow';
  if (stateVal === 'MANUAL_CONTROL') pulseClass = ' badge-pulse-purple';
  return `<span class="state-badge ${cls}${pulseClass}">${esc(stateVal || 'NEW')}</span>`;
}
