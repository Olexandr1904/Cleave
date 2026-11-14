// reviews.js — Reviews tab: per repo, the PRs where it is my turn and the ones waiting on others, in two
// tables with filter chips and a search; a row opens the PR in a side panel (report, jobs, replies).
// Read-only plus Review now, Skip and the reviews switches: nothing is posted from the dashboard.

import { loadReviewDetail, loadReviews, reviewNow, skipReview } from './api.js';
import { renderSwitchPanel } from './switches.js';
import { esc, fmtIso, timeAgo } from './helpers.js';

const JOB_LABEL = {
  queued: 'Queued', running: 'Running', done: 'Report ready', posted: 'Posted', failed: 'Failed',
  deferred: 'Deferred', superseded: 'Superseded', skipped: 'Skipped', stale: 'Stale', cancelled: 'Cancelled',
};
const TURN_CLASS = { review: 'turn-review', recheck: 'turn-recheck', merge: 'turn-merge' };
// The board's names for the report's finding kinds (orchestrator/reviewer/service.py's _FINDING_KINDS).
const KIND_OF = {
  red: 'critical', orange: 'major', yellow: 'minor', question: 'question', preexisting: 'preexisting',
};
const KIND_NAME = {
  critical: 'Critical', major: 'Major', minor: 'Minor', question: 'Question', preexisting: 'Pre-existing',
};
const FINDING_GROUPS = [
  ['Confirmed', ['red', 'orange', 'yellow']], ['Questions', ['question']], ['Pre-existing', ['preexisting']],
];
const EVENT_CLASS = { REQUEST_CHANGES: 'event-changes', APPROVE: 'event-approve' };
const TABLES = [['mine', 'My turn', 'my_turn'], ['waiting', 'Waiting on others', 'waiting']];

const VIEW_HTML = `<div class="reviews-view">
  <div class="reviews-main">
    <div id="switch-panel-container"></div>
    <div class="reviews-bar" hidden>
      <div class="reviews-chips"></div>
      <input class="reviews-search" name="reviews-search" type="search" placeholder="Search #, title, ticket" aria-label="Search PRs">
    </div>
    <div class="reviews-list"><div class="state-msg">Loading…</div></div>
    <div class="reviews-hint"><kbd>j</kbd> <kbd>k</kbd> move · <kbd>Enter</kbd> open · <kbd>Esc</kbd> close ·
      <kbd>/</kbd> search</div>
  </div>
  <div class="review-panel" hidden></div>
  <div class="reviews-status" role="status" aria-live="polite"></div>
</div>`;
// At this width the panel covers the screen: a modal dialog rather than a side panel.
const PHONE = window.matchMedia('(max-width: 760px)');

let repos = [];         // the last /api/reviews answer
let filter = 'all';     // the active chip
let openPr = null;      // {scope, number} of the PR in the side panel; null while it is closed
let focusKey = null;    // the row the j/k focus is on, as rowKey()
let panelOpen = {};     // the open PR's <details> as the operator left them, by data-key
let listLoads = 0;      // a newer load makes an older one's answer stale
let panelLoads = 0;
let statusTimer = null;

const rowKey = (scope, number) => `${scope}#${number}`;
const isQueued = job => job != null && (job.state === 'queued' || job.state === 'running');

function minutesSince(iso) {
  const t = Date.parse(iso);
  return isNaN(t) ? null : Math.max(0, Math.floor((Date.now() - t) / 60000));
}

function localTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });
}

function timeUntil(iso) {
  const t = Date.parse(iso);
  if (isNaN(t)) return '';
  const min = Math.max(0, Math.round((t - Date.now()) / 60000));
  return min < 60 ? `${min}m` : `${Math.floor(min / 60)}h`;
}

// ── List ──

function matches(pr, group, query) {
  const shown = filter === 'all' || filter === group || (filter === 'attention' && group === 'mine' && !isQueued(pr.job));
  return shown && (!query || `#${pr.number} ${pr.title} ${pr.ticket_key}`.toLowerCase().includes(query));
}

function chipsHtml() {
  const mine = repos.flatMap(r => r.my_turn);
  const waiting = repos.flatMap(r => r.waiting);
  const counts = {
    all: mine.length + waiting.length, attention: mine.filter(pr => !isQueued(pr.job)).length,
    mine: mine.length, waiting: waiting.length,
  };
  const chip = (key, label) => `<button class="reviews-chip" data-review-filter="${key}" data-fk="chip:${key}"
    aria-pressed="${filter === key}"><b>${counts[key]}</b> ${label}</button>`;
  const queue = [...mine, ...waiting].filter(pr => isQueued(pr.job)).length;
  return chip('all', 'all PRs') + chip('attention', 'need you') + chip('mine', 'my turn')
    + chip('waiting', 'waiting on others')
    + `<span class="reviews-chip static"><b>${queue}</b> in the job queue</span>`;
}

function flagsHtml(pr) {
  return [pr.pipeline && 'pipeline', pr.large && 'large', pr.draft && 'draft'].filter(Boolean)
    .map(flag => `<span class="reviews-flag">${flag}</span>`).join('');
}

function turnChips(pr) {
  return pr.my_turns.map(action =>
    `<span class="reviews-turn ${TURN_CLASS[action] || ''}">${esc(action)}</span>`).join('');
}

function turnHtml(pr, group) {
  const others = group === 'waiting' && pr.other_turns.length
    ? `<span class="reviews-sub">${pr.other_turns.map(t => `${esc(t.login)}: ${esc(t.action)}`).join(', ')}</span>`
    : '';
  return `<div class="reviews-turns">${turnChips(pr)}${others}</div>`;
}

function stateDot(state, label) {
  return `<span class="reviews-state is-${esc(state)}"><span class="reviews-dot"></span>${esc(label)}</span>`;
}

function stateHtml(pr) {
  const job = pr.job;
  if (job) {
    const label = job.state === 'done' && job.kind === 'recheck' ? 'Replies ready' : JOB_LABEL[job.state] || job.state;
    return stateDot(job.state, label);
  }
  return pr.skipped ? stateDot('skipped', 'Skipped') : '<span class="reviews-state is-none">—</span>';
}

// "1 critical, 2 questions, …": what the colored squares say, for a label and a tooltip.
function findingsText(counts) {
  return Object.keys(KIND_NAME).filter(kind => counts[kind]).map(kind => `${counts[kind]} ${
    kind === 'question' && counts[kind] !== 1 ? 'questions' : KIND_NAME[kind].toLowerCase()}`).join(', ')
    || 'no findings';
}

function findingsBar(findings) {
  const squares = Object.keys(KIND_NAME).map(kind =>
    `<i class="kind-${kind}" title="${KIND_NAME[kind]}"></i>`.repeat(findings[kind])).join('');
  const label = findingsText(findings);
  return `<span class="reviews-fbar" role="img" aria-label="${label}" title="${label}">${squares}</span>`;
}

function jobSubHtml(pr) {
  const job = pr.job;
  // At most two lines; the whole text in its tooltip. `text` is escaped already.
  const sub = text => `<span class="reviews-sub reviews-clamp" title="${text}">${text}</span>`;
  if (!job) return pr.large ? sub(`${esc(pr.changed_files)} files · no automatic review`) : '';
  if (job.state === 'running') {
    const min = minutesSince(job.started_at);
    return min == null ? '' : sub(`${min} min`);
  }
  if (job.state === 'deferred') return job.retry_at ? sub(`next try ${esc(localTime(job.retry_at))}`) : '';
  if (job.state === 'failed') return job.reason ? sub(esc(job.reason)) : '';
  if (job.state !== 'done') return '';
  const sent = job.message_sent ? 'sent to Telegram' : 'not delivered yet';
  const f = job.findings;
  if (!f) return sub(sent);
  return `<span class="reviews-findings">${findingsBar(f)}${sub(
    `${f.critical + f.major + f.minor} confirmed · ${f.question} questions · ${sent}`)}</span>`;
}

function actionsHtml(scope, pr, where) {
  const target = `data-scope="${esc(scope)}" data-number="${esc(pr.number)}"`;
  const fk = action => `data-fk="${where}:${action}:${esc(rowKey(scope, pr.number))}"`;
  // A skipped head can still have a queued or deferred review (Review now after Skip): Skip drops it.
  const pendingHere = pr.job && pr.job.kind === 'review'
    && (pr.job.state === 'queued' || pr.job.state === 'deferred') && pr.job.head === pr.head;
  const skip = pr.my_turns.includes('review') && (!pr.skipped || pendingHere)
    ? `<button class="reviews-btn" data-review-action="skip" ${target} ${fk('skip')} title="Skip this head">Skip</button>`
    : '';
  // Review now would be refused while a review of this head is queued, running or deferred.
  const atHead = pr.job && pr.job.kind === 'review' && pr.job.head === pr.head;
  const deferred = atHead && pr.job.state === 'deferred';
  const title = !deferred ? `A review of this head is ${esc(pr.job?.state)}`
    : pr.job.retry_at ? `Deferred; retries at ${esc(localTime(pr.job.retry_at))}` : 'Deferred; retries automatically';
  const now = atHead && (isQueued(pr.job) || deferred)
    ? `<button class="reviews-btn" disabled title="${title}">${JOB_LABEL[pr.job.state]}</button>`
    : `<button class="reviews-btn primary" data-review-action="review-now" ${target} ${fk('now')}>${
      pr.large ? 'Review anyway' : 'Review now'}</button>`;
  return `${now}${skip}
    <a class="reviews-btn ghost" href="${esc(pr.url)}" target="_blank" rel="noopener" ${fk('github')}
      aria-label="Open #${esc(pr.number)} on GitHub">GitHub ↗</a>`;
}

function rowHtml(scope, pr, group) {
  const key = rowKey(scope, pr.number);
  const sub = [pr.ticket_key && `<span class="reviews-mono">${esc(pr.ticket_key)}</span>`,
    esc(pr.roles.join(', ')), `${esc(pr.changed_files)} files`].filter(Boolean).join(' · ');
  return `<tr tabindex="0" data-review-row="${esc(key)}" data-fk="row:${esc(key)}"
      data-scope="${esc(scope)}" data-number="${esc(pr.number)}">
    <td><div class="reviews-pr">
      <div class="reviews-pr-line"><a class="reviews-num" href="${esc(pr.url)}" target="_blank" rel="noopener"
        data-fk="num:row:${esc(key)}">#${esc(pr.number)}</a><span class="reviews-title">${esc(pr.title)}</span>${flagsHtml(pr)}</div>
      <div class="reviews-sub">${sub}</div>
    </div></td>
    <td>${turnHtml(pr, group)}</td>
    <td><div class="reviews-job">${stateHtml(pr)}${jobSubHtml(pr)}</div></td>
    <td class="reviews-when">${esc(timeAgo(pr.last_change))}</td>
    <td><div class="reviews-actions">${actionsHtml(scope, pr, 'row')}</div></td>
  </tr>`;
}

function repoLineHtml(repo) {
  const polled = repo.last_poll_at ? ` · polled ${esc(timeAgo(repo.last_poll_at))}` : '';
  const next = repo.next_poll_at ? ` · next in ${esc(timeUntil(repo.next_poll_at))}` : '';
  const reason = repo.status === 'ok' || !repo.reason ? ''
    : repo.status === 'doc_missing' ? `reviews are off: ${repo.reason}` : repo.reason;
  return `<div class="reviews-repo-line"><span class="reviews-dot status-${esc(repo.status)}"></span>${
    esc(repo.scope)} · ${esc(repo.status)}${polled}${next}</div>
    ${reason ? `<div class="reviews-reason">${esc(reason)}</div>` : ''}`;
}

function repoHtml(repo, query) {
  const tables = TABLES.map(([group, title, field]) => {
    const prs = repo[field].filter(pr => matches(pr, group, query));
    if (!prs.length) return '';
    return `<h3 class="reviews-group-title">${title} <span>${prs.length}</span></h3>
      <div class="reviews-table-wrap"><table class="reviews-table">
        <thead><tr><th>Pull request</th><th>Turn</th><th>Job</th><th class="reviews-when">Last change</th>
          <th><span class="reviews-sr">Actions</span></th></tr></thead>
        <tbody>${prs.map(pr => rowHtml(repo.scope, pr, group)).join('')}</tbody>
      </table></div>`;
  }).join('');
  const empty = repo.my_turn.length + repo.waiting.length ? 'No PR matches.' : 'No open PRs on the board.';
  return `<section class="reviews-repo">${repoLineHtml(repo)}${tables || `<div class="reviews-empty">${empty}</div>`}</section>`;
}

// The open row and the j/k row as classes; a row that left the list keeps neither.
function markRows(view) {
  const open = openPr && rowKey(openPr.scope, openPr.number);
  view.querySelectorAll('[data-review-row]').forEach(row => {
    row.classList.toggle('open', row.dataset.reviewRow === open);
    row.classList.toggle('focus', row.dataset.reviewRow === focusKey);
  });
}

// Replace `el`'s content, keeping its scroll and the keyboard focus on an element that is redrawn.
function redraw(el, html) {
  const fk = el.contains(document.activeElement) ? document.activeElement.dataset.fk : null;
  const top = el.scrollTop;
  el.innerHTML = html;
  el.scrollTop = top;
  if (fk) el.querySelector(`[data-fk="${CSS.escape(fk)}"]`)?.focus({ preventScroll: true });
}

function paintList(view) {
  const bar = view.querySelector('.reviews-bar');
  bar.hidden = !repos.length;
  redraw(view.querySelector('.reviews-chips'), chipsHtml());
  const query = view.querySelector('.reviews-search').value.trim().toLowerCase();
  redraw(view.querySelector('.reviews-list'), repos.length
    ? repos.map(repo => repoHtml(repo, query)).join('')
    : '<div class="state-msg">No repo has reviews enabled.</div>');
  markRows(view);
}

// ── Side panel ──

function findPr(target) {
  const repo = repos.find(r => r.scope === target.scope);
  return repo && [...repo.my_turn, ...repo.waiting].find(pr => pr.number === target.number);
}

function panelHeadHtml(target, pr) {
  const back = '<button class="reviews-btn ghost" data-review-close data-fk="close">← Back</button>';
  if (!pr) {
    return `<div class="review-panel-head"><div class="review-panel-row">${back}
      <span class="reviews-sub" id="review-panel-title">${esc(target.scope)} #${esc(target.number)} · not on the board</span>
    </div></div>`;
  }
  return `<div class="review-panel-head">
    <div class="review-panel-row">${back}
      <span class="reviews-sub">${esc(target.scope)}</span>
      <a class="reviews-num" href="${esc(pr.url)}" target="_blank" rel="noopener"
        data-fk="num:panel:${esc(rowKey(target.scope, pr.number))}">#${esc(pr.number)}</a>
      ${pr.ticket_key ? `<span class="reviews-mono">${esc(pr.ticket_key)}</span>` : ''}
      <span class="reviews-sub">head <code>${esc(pr.head)}</code> · ${esc(pr.changed_files)} files</span>
    </div>
    <h3 class="review-panel-title" id="review-panel-title">${esc(pr.title)}${flagsHtml(pr)}</h3>
    <div class="review-panel-row">${turnChips(pr)}${stateHtml(pr)}<span class="review-spacer"></span>
      <div class="reviews-actions">${actionsHtml(target.scope, pr, 'panel')}</div></div>
  </div>`;
}

function section(title, count, body) {
  const n = count == null ? '' : ` <span>${count}</span>`;
  return `<div class="review-sec">${title ? `<h4>${title}${n}</h4>` : ''}${body}</div>`;
}

function details(key, defaultOpen, summary, body, cls) {
  const open = key in panelOpen ? panelOpen[key] : defaultOpen;
  return `<details class="${cls}" data-key="${esc(key)}"${open ? ' open' : ''}>
    <summary data-fk="sum:${esc(key)}">${summary}</summary>${body}</details>`;
}

// The body without its first **File:** line: the summary already names the path.
function itemBody(body) {
  const lines = body.split('\n');
  const file = lines.findIndex(line => line.startsWith('**File:**'));
  if (file >= 0) lines.splice(file, 1);
  return lines.join('\n').trim();
}

function findingHtml(jobId, item) {
  const kind = KIND_OF[item.kind];
  const where = item.path ? `${item.path}${item.line == null ? '' : `:${item.line}`}` : '';
  const meta = [where, item.note].filter(Boolean).map(esc).join(' · ');
  const body = itemBody(item.body);
  return details(`${jobId}:${item.number}`, item.kind === 'red',
    `<span class="review-sq kind-${kind}" title="${KIND_NAME[kind]}"></span>
      <span class="review-n">${esc(item.number)}.</span><span class="review-t">${esc(item.title)}</span>
      <span class="review-badge">open</span>${meta ? `<span class="review-where">${meta}</span>` : ''}`,
    body ? `<div class="review-body">${esc(body)}</div>` : '', 'review-finding');
}

// Delivery only: the run's other notes (a worktree left behind, …) are in the jobs table.
function telegramText(job) {
  if (job.message_sent) {
    const parts = job.message_ids.length;
    return `sent in ${parts} part${parts === 1 ? '' : 's'}`;
  }
  if (job.state !== 'done') return `not sent: the job ended ${job.state}`;  // its PR moved on
  const note = job.notes.find(n => n.startsWith('message not delivered'));
  return note ? `not delivered yet · ${note}` : 'not delivered yet';
}

function reportHtml(detail) {
  const report = detail.report;
  if (!report) {
    return section('', null, '<div class="reviews-reason">The report could not be read; the raw report is below.</div>');
  }
  const job = detail.jobs.find(j => j.id === detail.report_job);
  const kinds = report.items.map(item => KIND_OF[item.kind]);
  const counts = Object.fromEntries(Object.keys(KIND_NAME).map(kind => [kind, kinds.filter(k => k === kind).length]));
  const legend = Object.keys(KIND_NAME).map(kind => counts[kind]
    ? `<span><i class="kind-${kind}"></i>${KIND_NAME[kind]} <b>${counts[kind]}</b></span>` : '',
  ).join('') + `<span>Dropped <b>${report.dropped.length}</b></span>`;
  const meter = kinds.map(k => `<i class="kind-${k}" title="${KIND_NAME[k]}"></i>`).join('');
  const verdict = `<div class="review-verdict">
      <div><span class="review-event ${EVENT_CLASS[report.event] || ''}">${esc(report.event)}</span>
        <span class="reviews-sub">suggested · ${esc(report.event_reason)}</span></div>
      ${report.summary ? `<p>${esc(report.summary)}</p>` : ''}
    </div>
    ${kinds.length ? `<div class="review-meter" role="img" aria-label="${findingsText(counts)}">${meter}</div>` : ''}
    <div class="review-legend">${legend}</div>
    ${job ? `<div class="reviews-sub">Telegram: ${esc(telegramText(job))}</div>` : ''}`;
  const groups = FINDING_GROUPS.map(([title, group]) => {
    const items = report.items.filter(item => group.includes(item.kind));
    return items.length ? section(title, items.length, items.map(i => findingHtml(detail.report_job, i)).join('')) : '';
  }).join('');
  const dropped = report.dropped.length ? section('Dropped (never posted)', report.dropped.length,
    `<ul class="review-dropped">${report.dropped.map(d =>
      `<li>${esc(d.number)}. ${esc(d.text)} — ${esc(d.why)}</li>`).join('')}</ul>`) : '';
  return section('', null, verdict) + (groups || section('', null, '<div class="reviews-empty">No findings.</div>'))
    + dropped;
}

function replyTarget(entry) {
  if (entry.thread_id != null) return `thread ${entry.thread_id}`;
  if (entry.review_id != null) return `review ${entry.review_id}`;
  return '';
}

function repliesHtml(detail) {
  const title = `Replies <span>${detail.replies.length}</span> · job ${esc(detail.replies_job)}`;
  if (!detail.replies.length) return section(title, null, '<div class="reviews-empty">No replies drafted.</div>');
  const rows = detail.replies.map(entry => `<tr>
    <td>${esc(entry.n)}</td><td>${esc(replyTarget(entry))}</td><td class="review-body">${esc(entry.body)}</td>
  </tr>`).join('');
  return section(title, null, `<div class="reviews-table-wrap"><table class="review-jobs">
    <thead><tr><th>N</th><th>Target</th><th>Body</th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

// Why a PR has no report yet, from its newest job.
function noReportText(pr, jobs) {
  const job = jobs[0];
  if (!job) {
    if (pr && pr.large) return `No report: ${pr.changed_files} files, so no automatic review.`;
    return pr && pr.skipped ? 'No report: this head is skipped.' : 'No report yet.';
  }
  const what = job.kind === 'recheck' ? 're-check' : 'review';
  if (job.state === 'queued') return `No report yet: the ${what} is queued.`;
  if (job.state === 'running') return `No report yet: the ${what} is running.`;
  if (job.state === 'deferred') return `No report yet: deferred until ${localTime(job.retry_at)}.`;
  if (job.state === 'failed') return `No report: failed: ${job.reason}`;
  return `No report: the ${what} ended ${job.state}.`;
}

function durationText(seconds) {
  if (seconds == null) return '';
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function jobRowHtml(job) {
  // fmtIso escapes a stamp it cannot parse.
  const nextTry = job.state === 'deferred' && job.retry_at ? ` next try ${fmtIso(job.retry_at)}` : '';
  const reason = [job.reason, ...job.notes].filter(Boolean).map(esc).join('<br>');
  const cost = job.finished_at ? `$${Number(job.cost_usd).toFixed(2)}` : '';
  return `<tr>
    <td>${esc(job.kind)}</td>
    <td>${stateDot(job.state, job.state)}${nextTry}</td>
    <td>${reason}</td>
    <td><code>${esc(job.head)}</code></td>
    <td>${esc(timeAgo(job.started_at))}</td>
    <td>${esc(durationText(job.duration_s))}</td>
    <td>${esc(cost)}</td>
    <td>${esc(job.message_ids.join(', '))}</td>
  </tr>`;
}

function jobsHtml(jobs) {
  if (!jobs.length) return section('Jobs', null, '<div class="reviews-empty">No jobs.</div>');
  return section('Jobs', jobs.length, `<div class="reviews-table-wrap"><table class="review-jobs">
    <thead><tr>
      <th>Kind</th><th>State</th><th>Reason</th><th>Head</th><th>Started</th><th>Duration</th>
      <th>Cost (API-equivalent)</th><th>Telegram messages</th>
    </tr></thead>
    <tbody>${jobs.map(jobRowHtml).join('')}</tbody>
  </table></div>`);
}

function panelBodyHtml(pr, detail) {
  const at = id => detail.jobs.findIndex(j => j.id === id);
  // A re-check newer than the report shows its replies instead of the findings.
  const replies = detail.replies_job && (!detail.report_job || at(detail.replies_job) < at(detail.report_job));
  const main = replies ? repliesHtml(detail)
    : detail.report_job ? reportHtml(detail)
      : section('', null, `<div class="review-empty">${esc(noReportText(pr, detail.jobs))}</div>`);
  const raw = detail.report_text == null ? '' : section('', null, details('raw', false,
    `Raw report · job ${esc(detail.report_job)}`, `<pre class="report-content">${esc(detail.report_text)}</pre>`,
    'review-raw'));
  return main + jobsHtml(detail.jobs) + raw;
}

// A side panel beside the list; a modal dialog where it covers the screen.
function setPanelRole(panel, target) {
  if (PHONE.matches) {
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', 'review-panel-title');
    panel.removeAttribute('aria-label');
  } else {
    panel.setAttribute('role', 'complementary');
    panel.setAttribute('aria-label', `Pull request #${target.number}`);
    panel.removeAttribute('aria-modal');
    panel.removeAttribute('aria-labelledby');
  }
}

async function paintPanel(view) {
  const panel = view.querySelector('.review-panel');
  const target = openPr;
  const seq = ++panelLoads;
  view.classList.toggle('has-panel', Boolean(target));
  if (!target) {
    panel.hidden = true;
    panel.innerHTML = '';
    return;
  }
  const key = rowKey(target.scope, target.number);
  if (panel.dataset.pr !== key) {  // another PR: its header at once, its body once loaded
    panel.dataset.pr = key;
    panel.hidden = false;
    panel.innerHTML = panelHeadHtml(target, findPr(target))
      + section('', null, '<div class="reviews-empty">Loading…</div>');
    panel.scrollTop = 0;
  }
  setPanelRole(panel, target);
  let detail, error;
  try {
    detail = await loadReviewDetail(target.scope, target.number);
  } catch (e) {
    error = e;
  }
  if (seq !== panelLoads || !view.isConnected) return;  // closed, another PR opened or a newer load meanwhile
  panel.querySelectorAll('details[data-key]').forEach(d => { panelOpen[d.dataset.key] = d.open; });
  const pr = findPr(target);
  redraw(panel, panelHeadHtml(target, pr) + (error
    ? section('', null, `<div class="reviews-error">Error: ${esc(String(error))}</div>`)
    : panelBodyHtml(pr, detail)));
}

// Opening moves the focus to the panel's Back; closing returns it to the row.
function openRow(view, row) {
  openPr = { scope: row.dataset.scope, number: Number(row.dataset.number) };
  focusKey = row.dataset.reviewRow;
  panelOpen = {};
  markRows(view);
  paintPanel(view);  // its header is drawn before it awaits the detail
  view.querySelector('.review-panel [data-review-close]').focus();
}

function closePanel(view) {
  const key = rowKey(openPr.scope, openPr.number);
  openPr = null;
  delete view.querySelector('.review-panel').dataset.pr;
  markRows(view);
  paintPanel(view);
  view.querySelector(`[data-review-row="${CSS.escape(key)}"]`)?.focus();
}

// ── Events ──

// A line in the view's status region: what an action did, or why it failed.
function showStatus(view, text, failed) {
  const status = view.querySelector('.reviews-status');
  status.textContent = text;
  status.classList.toggle('is-error', failed);
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { status.textContent = ''; }, failed ? 10000 : 6000);
}

async function runAction(view, btn) {
  const { reviewAction, scope, number } = btn.dataset;
  const skip = reviewAction === 'skip';
  const name = btn.textContent.trim();
  const pr = findPr({ scope, number: Number(number) });
  const inPanel = btn.closest('.review-panel') !== null;  // asked now: the redraw replaces the button
  btn.disabled = true;
  btn.textContent = skip ? 'Skipping…' : 'Queuing…';
  let text, failed = false;
  try {
    if (skip) {
      await skipReview(scope, Number(number));
      text = `Skipped this head of #${number}${pr ? ` (${pr.head})` : ''}`;
    } else {
      const { job } = await reviewNow(scope, Number(number));
      text = `Queued a review of #${number} at ${job.head}`;
    }
  } catch (err) {
    text = `${name} of #${number} failed: ${err.message}`;
    failed = true;
  }
  if (!view.isConnected) return;  // the operator left the Reviews view meanwhile: nothing to show it in
  showStatus(view, text, failed);
  await renderReviews();
  // The clicked button lost the focus (disabled, then redrawn as Queued or gone): the panel's Back for a
  // button in the panel, else the PR's row.
  if (!view.contains(document.activeElement)) {
    const back = inPanel && openPr && view.querySelector('.review-panel [data-review-close]');
    (back || view.querySelector(`[data-review-row="${CSS.escape(rowKey(scope, Number(number)))}"]`))?.focus();
  }
}

function onClick(view, e) {
  const chip = e.target.closest('[data-review-filter]');
  if (chip) {
    filter = chip.dataset.reviewFilter;
    paintList(view);
    return;
  }
  const action = e.target.closest('[data-review-action]');
  if (action) {
    runAction(view, action);
    return;
  }
  if (e.target.closest('[data-review-close]')) {
    closePanel(view);
    return;
  }
  if (e.target.closest('a, button, input, summary')) return;
  const row = e.target.closest('[data-review-row]');
  if (row) openRow(view, row);
}

function moveFocus(view, step) {
  const rows = [...view.querySelectorAll('[data-review-row]')];
  if (!rows.length) return;
  const at = rows.findIndex(row => row.dataset.reviewRow === focusKey);
  const row = rows[at < 0 ? 0 : Math.min(Math.max(at + step, 0), rows.length - 1)];
  focusKey = row.dataset.reviewRow;
  markRows(view);
  row.focus();
}

// While the panel covers the screen, Tab cycles through its controls only.
function keepTabInPanel(view, e) {
  const panel = view.querySelector('.review-panel');
  const items = [...panel.querySelectorAll('a[href], button:not([disabled]), summary')];
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const at = document.activeElement;
  if (!panel.contains(at) || at === (e.shiftKey ? first : last)) {
    e.preventDefault();
    (e.shiftKey ? last : first).focus();
  }
}

function onKey(e) {
  const view = document.querySelector('.reviews-view');
  if (!view || document.querySelector('.dialog-overlay, .modal:not(.hidden)')) return;
  const modal = openPr && PHONE.matches;
  if (e.key === 'Tab' && modal) {
    keepTabInPanel(view, e);
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.target.closest('input, textarea, select, [contenteditable]')) return;
  if (modal && e.key !== 'Escape') return;  // the list is behind the panel
  if (e.key === 'j' || e.key === 'k') {
    moveFocus(view, e.key === 'j' ? 1 : -1);
  } else if (e.key === 'Enter' && (e.target === document.body || e.target.matches('[data-review-row]'))) {
    e.preventDefault();  // else the key reaches Back, focused by openRow, and clicks it
    const row = view.querySelector(`[data-review-row="${CSS.escape(focusKey || '')}"]`);
    if (row) openRow(view, row);
  } else if (e.key === 'Escape' && openPr) {
    closePanel(view);
  } else if (e.key === '/') {
    e.preventDefault();
    view.querySelector('.reviews-search').focus();
  }
}

let keysBound = false;

function mount(content) {
  content.innerHTML = VIEW_HTML;
  const view = content.querySelector('.reviews-view');
  repos = [];
  filter = 'all';
  openPr = null;
  focusKey = null;
  panelOpen = {};
  view.addEventListener('click', e => onClick(view, e));
  view.addEventListener('focusin', e => {
    const row = e.target.closest('[data-review-row]');
    if (row && row.dataset.reviewRow !== focusKey) {
      focusKey = row.dataset.reviewRow;
      markRows(view);
    }
  });
  view.querySelector('.reviews-search').addEventListener('input', () => paintList(view));
  if (!keysBound) {
    document.addEventListener('keydown', onKey);
    PHONE.addEventListener('change', () => {
      const panel = document.querySelector('.reviews-view .review-panel');
      if (panel && openPr) setPanelRole(panel, openPr);
    });
    keysBound = true;
  }
  return view;
}

export async function renderReviews() {
  const content = document.getElementById('content');
  // Auto-refresh and Refresh redraw the view in place: the open panel, its expanded findings, the
  // search text and the row focus stay. Once another view has replaced it, it starts afresh.
  const view = content.querySelector('.reviews-view') || mount(content);
  const seq = ++listLoads;
  let data, error;
  try {
    data = await loadReviews();
  } catch (e) {
    error = e;
  }
  if (seq !== listLoads || !view.isConnected) return;
  if (error) {
    view.querySelector('.reviews-list').innerHTML =
      `<div class="state-msg reviews-error">Error: ${esc(String(error))}</div>`;
    return;
  }
  repos = data.repos;
  renderSwitchPanel(view.querySelector('#switch-panel-container'), 'reviews');
  paintList(view);
  await paintPanel(view);
}
