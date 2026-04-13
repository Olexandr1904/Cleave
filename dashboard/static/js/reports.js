// reports.js — report tab loading and display

import { loadReport } from './api.js';
import { esc } from './helpers.js';

export function renderReportTabs(ticketId, reports, meta) {
  const allFiles = [];
  (reports || []).forEach(f => allFiles.push({ name: f, folder: 'reports' }));
  (meta || []).forEach(f => allFiles.push({ name: f, folder: 'meta' }));

  if (allFiles.length === 0) return '';

  const tabBar = allFiles.map(r =>
    `<button class="tab-btn" data-ticket="${esc(ticketId)}" data-file="${esc(r.name)}" data-folder="${esc(r.folder)}"
      >${esc(r.name)}</button>`
  ).join('');

  return `<div class="detail-section">
    <div class="detail-section-title">Reports &amp; Files</div>
    <div class="tab-bar" id="report-tabs">${tabBar}</div>
    <div id="report-content-area"><div style="color:#6e7681;font-size:12px;">Select a file to view.</div></div>
  </div>`;
}

export function bindReportTabClicks() {
  const tabs = document.getElementById('report-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', async (e) => {
    const btn = e.target.closest('.tab-btn');
    if (!btn) return;
    const ticketId = btn.dataset.ticket;
    const file = btn.dataset.file;
    const folder = btn.dataset.folder;

    // Update active tab
    tabs.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    const area = document.getElementById('report-content-area');
    area.innerHTML = '<div style="color:#6e7681;font-size:12px;padding:8px 0;">Loading…</div>';

    try {
      const text = await loadReport(ticketId, file, folder);
      area.innerHTML = `<div class="report-content">${esc(text)}</div>`;
    } catch (err) {
      area.innerHTML = `<div style="color:#f85149;font-size:12px;">Error: ${esc(String(err))}</div>`;
    }
  });
}
