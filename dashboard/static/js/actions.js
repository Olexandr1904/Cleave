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

export async function clearGradleAndRetry(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/clear-gradle-and-retry`);
}

export async function takeControl(ticketId, confirm = false) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/take-control`, { confirm });
}

export async function releaseControl(ticketId, comment = '') {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/release-control`, { comment });
}

export async function resumeWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/resume`);
}

export async function archiveWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/archive`);
}

export function rerunWorkspace(ticketId) {
  return new Promise((resolve, reject) => {
    const overlay = document.createElement('div');
    overlay.className = 'dialog-overlay';
    overlay.innerHTML = `<div class="dialog">
      <div class="dialog-title">Rerun pipeline for ${esc(ticketId)}?</div>
      <div>
        <p style="margin:0 0 8px;font-size:12px;color:#8b949e;">Describe why this ticket needs to be rerun. This will be passed to the BA agent as context.</p>
        <textarea id="rerun-reason" class="manual-comment" placeholder="e.g. Post-QA regression: login crashes on Android 14" style="width:100%;min-height:80px;"></textarea>
      </div>
      <div class="dialog-actions">
        <button class="action-btn btn-retry" id="dlg-cancel">Cancel</button>
        <button class="action-btn btn-take-control" id="dlg-confirm" disabled>Rerun</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);

    const textarea = overlay.querySelector('#rerun-reason');
    const confirmBtn = overlay.querySelector('#dlg-confirm');

    textarea.addEventListener('input', () => {
      confirmBtn.disabled = textarea.value.trim().length === 0;
    });

    overlay.querySelector('#dlg-cancel').onclick = () => {
      overlay.remove();
      reject(new Error('cancelled'));
    };
    confirmBtn.onclick = async () => {
      const reason = textarea.value.trim();
      overlay.remove();
      try {
        const result = await postJSON(
          `/api/workspaces/${encodeURIComponent(ticketId)}/rerun`,
          { reason },
        );
        resolve(result);
      } catch (e) {
        reject(e);
      }
    };
    overlay.onclick = (e) => { if (e.target === overlay) { overlay.remove(); reject(new Error('cancelled')); } };
  });
}

export async function pauseWorkspace(ticketId, confirm = false) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/pause`, { confirm });
}

export async function unpauseWorkspace(ticketId) {
  return postJSON(`/api/workspaces/${encodeURIComponent(ticketId)}/unpause`);
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
