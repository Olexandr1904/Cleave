// actions.js — action button handlers and confirmation dialogs

import { postJSON } from './api.js';
import { esc } from './helpers.js';

export async function approveWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/approve`);
}

export async function rejectWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/reject`);
}

export async function retryWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/retry`);
}

export async function takeControl(ticketId, confirm = false) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/take-control`, { confirm });
}

export async function releaseControl(ticketId, comment = '') {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/release-control`, { comment });
}

export async function setMode(mode) {
  return postJSON('/api/daemon/mode', { mode });
}

export function showConfirmDialog(title, bodyHtml, confirmLabel, onConfirm) {
  const overlay = document.createElement('div');
  overlay.className = 'dialog-overlay';
  overlay.innerHTML = `<div class="dialog">
    <div class="dialog-title">${esc(title)}</div>
    <div>${bodyHtml}</div>
    <div class="dialog-actions">
      <button class="action-btn btn-retry" id="dlg-cancel">Cancel</button>
      <button class="action-btn btn-take-control" id="dlg-confirm">${esc(confirmLabel)}</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);

  overlay.querySelector('#dlg-cancel').onclick = () => overlay.remove();
  overlay.querySelector('#dlg-confirm').onclick = async () => {
    overlay.remove();
    await onConfirm();
  };
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
}
