// comparison.js — Comparison tab: the tally of the operator's verdicts on tickets run by both flows, then one
// row per comparison with both sides. Read-only: verdicts are given in Telegram, never here.
// Every value from the server goes in through textContent.

import { loadComparisons } from './api.js';

const TALLY = [['skill', 'Skill'], ['staged', 'Staged'], ['neither', 'Neither'], ['undecided', 'Undecided']];
const VERDICT_LABEL = { skill: 'Skill', staged: 'Staged', neither: 'Neither' };
const STATE_LABEL = { open: 'Open', deciding: 'Deciding', stuck: 'Stuck', closed: 'Closed' };
const COLUMNS = ['Ticket', 'Staged', 'Skill', 'Verdict', 'Note'];

let loads = 0;  // a newer load makes an older one's answer stale

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function duration(minutes) {
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

const count = n => Number(n).toLocaleString('en-US');

function tallyNode(tally) {
  const box = el('div', 'comparison-tally');
  for (const [key, label] of TALLY) {
    const tile = el('div', `comparison-tile is-${key}`);
    tile.append(el('b', '', String(tally[key] ?? 0)), el('span', '', label));
    box.append(tile);
  }
  return box;
}

function ticketCell(row) {
  const td = el('td', 'comparison-ticket');
  td.append(el('div', 'comparison-key', row.ticket_id));
  if (row.summary) td.append(el('div', 'comparison-summary', row.summary));
  td.append(el('div', 'comparison-sub', [row.scope, row.skill].filter(Boolean).join(' · ')));
  return td;
}

function sideCell(side) {
  const td = el('td', 'comparison-side');
  td.append(el('div', `comparison-state${side.state === 'gone' ? ' is-gone' : ''}`, side.state));
  if (side.pr_number) {
    const pr = el('a', 'comparison-pr', `#${side.pr_number}`);
    // Only a web address becomes a link: the URL comes from a forge, not from the dashboard.
    if (/^https?:\/\//.test(side.pr_url)) {
      pr.href = side.pr_url;
      pr.target = '_blank';
      pr.rel = 'noopener noreferrer';
    }
    td.append(pr);
  }
  if (side.state !== 'gone') {
    td.append(el('div', 'comparison-sub', `${duration(side.minutes)} · ${count(side.tokens_in)} in · ${
      count(side.tokens_out)} out tokens`));
  }
  return td;
}

function verdictCell(row) {
  const td = el('td', 'comparison-verdict');
  if (row.state === 'closed') {
    td.append(el('span', `comparison-badge is-${row.verdict || 'none'}`, VERDICT_LABEL[row.verdict] || '—'));
    if (row.decided_at) td.append(el('div', 'comparison-sub', `${row.decided_at.slice(0, 16).replace('T', ' ')} UTC`));
    return td;
  }
  td.append(el('span', `comparison-badge is-${row.state}`, STATE_LABEL[row.state] || row.state));
  if (VERDICT_LABEL[row.verdict]) td.append(el('div', 'comparison-sub', `${VERDICT_LABEL[row.verdict]} chosen`));
  if (row.state === 'stuck' && row.reason) td.append(el('div', 'comparison-reason', row.reason));
  return td;
}

function rowNode(row) {
  const tr = el('tr');
  const cells = [ticketCell(row), sideCell(row.staged_side), sideCell(row.skill_side), verdictCell(row),
    el('td', 'comparison-note', row.note)];
  // The column names, for the stacked rows at phone width.
  cells.forEach((td, i) => { td.dataset.label = COLUMNS[i]; });
  tr.append(...cells);
  return tr;
}

function tableNode(rows) {
  if (!rows.length) return el('div', 'state-msg', 'No comparisons yet.');
  const wrap = el('div', 'comparison-table-wrap');
  const table = el('table', 'comparison-table');
  const head = el('tr');
  COLUMNS.forEach(name => head.append(el('th', '', name)));
  const thead = el('thead');
  thead.append(head);
  const tbody = el('tbody');
  tbody.append(...rows.map(rowNode));
  table.append(thead, tbody);
  wrap.append(table);
  return wrap;
}

export async function renderComparison() {
  const content = document.getElementById('content');
  // Auto-refresh and Refresh redraw the view in place; once another view has replaced it, it starts afresh.
  let view = content.querySelector('.comparison-view');
  if (!view) {
    view = el('div', 'comparison-view');
    view.append(el('div', 'state-msg', 'Loading…'));
    content.replaceChildren(view);
  }
  const seq = ++loads;
  let data, error;
  try {
    data = await loadComparisons();
  } catch (e) {
    error = e;
  }
  if (seq !== loads || !view.isConnected) return;
  if (error) {
    view.replaceChildren(el('div', 'state-msg comparison-error', `Error: ${error}`));
    return;
  }
  view.replaceChildren(tallyNode(data.tally), tableNode(data.rows));
}
