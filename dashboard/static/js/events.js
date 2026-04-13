// events.js — event log rendering

import { esc, fmtTs, badgeClass } from './helpers.js';

export function renderEventsHtml(events, compact) {
  if (!events || events.length === 0) {
    return '<div class="state-msg">No events found.</div>';
  }

  return events.map(ev => {
    const cls = badgeClass(ev.event_type);
    const parts = [];
    if (!compact && ev.project_id) parts.push(`project: ${esc(ev.project_id)}`);
    if (!compact && ev.ticket_id)  parts.push(`ticket: ${esc(ev.ticket_id)}`);
    if (ev.agent_id) parts.push(`agent: ${esc(ev.agent_id)}`);
    if (ev.data && ev.data.duration != null) parts.push(`${ev.data.duration.toFixed(1)}s`);
    if (ev.data && ev.data.input_tokens != null) parts.push(`${ev.data.input_tokens}/${ev.data.output_tokens} tok`);

    return `<div class="event-row">
      <span class="event-ts">${fmtTs(ev.timestamp)}</span>
      <span class="event-badge ${cls}">${esc(ev.event_type)}</span>
      <div class="event-body">
        <div class="event-msg">${esc(ev.message)}</div>
        ${parts.length ? `<div class="event-meta">${parts.map(p => `<span>${p}</span>`).join('')}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}
