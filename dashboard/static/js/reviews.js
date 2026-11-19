// reviews.js — Reviews tab: one board of the repos with reviews, in four columns (To review · To fix · With others ·
// To merge) under a status line per repo, a repo filter, four tiles and a search; a card opens the PR in a side
// panel (report, replies, verdicts, posts, jobs). Read-only plus Review now, Skip, Fix now, Fix findings… and the
// reviews switches: nothing is posted from the dashboard. The board's model (columns, tiles, cards, buttons) is
// reviews-board.js.

import { fixFindings, fixNow, loadReviewDetail, loadReviews, reviewNow, skipReview } from './api.js';
import { showConfirmDialog } from './actions.js';
import { renderSwitchPanel } from './switches.js';
import { esc, fmtIso, timeAgo } from './helpers.js';
import {
  COLUMNS, againConfirm, avatarsHtml, boardColumns, boardTiles, buttonsHtml, cardHtml, cardTitle, columnOf,
  findingsScale, jobLineHtml, moveText, panelParts, repoFilterOf, ticketLinkHtml, verdictsHtml,
} from './reviews-board.js';

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
// A finding's or a reply's state (service.py's detail): its badge shows the word, its tooltip what it means.
const NUMBER_STATE = {
  posted: 'Landed on GitHub', ticked: 'Named in a post that did not land: see Posts', open: 'Not posted',
};
const GITHUB = 'https://github.com/';  // a post's link gets an href only to GitHub
const TILES = [
  ['needYou', 'Need you'], ['critical', 'Critical in your PRs'], ['waiting', 'Waiting on others'],
  ['longest', 'Longest wait'],
];
// What each card or panel button calls; the "…again" ones and Fix findings… ask first (againConfirm).
const CALLS = {
  'review-now': reviewNow, 'review-again': reviewNow, 'self-review': reviewNow, 'self-review-again': reviewNow,
  'fix-now': fixNow, 'fix-again': fixNow, 'fix-findings': fixFindings, 'fix-findings-again': fixFindings,
  skip: skipReview,
};

const VIEW_HTML = `<div class="reviews-view">
  <div class="reviews-main">
    <div id="switch-panel-container"></div>
    <div class="reviews-top" hidden>
      <div class="reviews-status-lines"></div>
      <input class="reviews-search" name="reviews-search" type="search" placeholder="Search #, title, ticket" aria-label="Search PRs">
    </div>
    <nav class="reviews-repos" aria-label="Repos"></nav>
    <div class="reviews-tiles"></div>
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
let repoScope = null;   // the repo the hash names (#/reviews/<scope>); null: every repo
let openPr = null;      // {scope, number} of the PR in the side panel; null while it is closed
let focusKey = null;    // the card the j/k focus is on, as rowKey()
let panelOpen = {};     // the open PR's <details> as the operator left them, by data-key
let listLoads = 0;      // a newer load makes an older one's answer stale
let panelLoads = 0;
let statusTimer = null;

const rowKey = (scope, number) => `${scope}#${number}`;

// The repo filter, from the router (#/reviews/<scope>); an unknown scope shows every repo.
export function setReviewsRepo(scope) {
  repoScope = scope || null;
}

function localTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });
}

// ── Board ──

const prsOf = repo => [...repo.my_turn, ...repo.waiting];

function matches(pr, query) {
  return `#${pr.number} ${pr.title} ${pr.ticket_key}`.toLowerCase().includes(query);
}

function stateDot(state, label) {
  return `<span class="reviews-state is-${esc(state)}"><span class="reviews-dot"></span>${esc(label)}</span>`;
}

// "1 critical, 2 questions, …": what the report's meter says, for its label.
function findingsText(counts) {
  return Object.keys(KIND_NAME).filter(kind => counts[kind]).map(kind => `${counts[kind]} ${
    kind === 'question' && counts[kind] !== 1 ? 'questions' : KIND_NAME[kind].toLowerCase()}`).join(', ')
    || 'no findings';
}

// One line per repo: its state, when it was last read, its jobs; a reason under it when it is not ok.
function statusLineHtml(repo) {
  const prs = prsOf(repo);
  const running = prs.filter(pr => pr.job?.state === 'running').length;
  const queued = prs.filter(pr => pr.job?.state === 'queued').length;
  const jobs = running || queued
    ? [running && `${running} running`, queued && `${queued} queued`].filter(Boolean).join(' · ') : 'no jobs running';
  const parts = [esc(repo.scope), repo.status === 'ok' ? '' : esc(repo.status),
    repo.last_poll_at ? `synced ${esc(timeAgo(repo.last_poll_at))}` : '', jobs].filter(Boolean).join(' · ');
  const reason = repo.status === 'ok' || !repo.reason ? ''
    : repo.status === 'doc_missing' ? `reviews are off: ${repo.reason}` : repo.reason;
  return `<div class="reviews-repo-line"><span class="reviews-dot status-${esc(repo.status)}"></span><span>${parts}</span></div>
    ${reason ? `<div class="reviews-reason">${esc(reason)}</div>` : ''}`;
}

// With more than one repo: All, then each repo with its number of PRs. A chip is a link: the router sets the filter.
function repoChipsHtml() {
  if (repos.length < 2) return '';
  const active = repoFilterOf(repoScope, repos);
  const chip = (scope, label, count) => `<a class="reviews-repo-chip" href="#/reviews${scope ? `/${esc(scope)}` : ''}"
    aria-current="${active === scope ? 'page' : 'false'}" data-fk="repo:${esc(scope || 'all')}">${esc(label)} <b>${count}</b></a>`;
  return chip(null, 'All', repos.reduce((n, r) => n + prsOf(r).length, 0))
    + repos.map(r => chip(r.scope, r.scope.split('/').pop(), prsOf(r).length)).join('');
}

function tilesHtml(tiles) {
  return TILES.map(([key, label]) => {
    const t = tiles[key];
    return `<div class="reviews-tile${t.alert ? ' is-alert' : ''}"><div class="reviews-tile-label">${label}</div>
      <div class="reviews-tile-value">${esc(t.value)}</div><div class="reviews-tile-sub">${esc(t.sub) || '&nbsp;'}</div></div>`;
  }).join('');
}

// The four columns of the shown repos' PRs; search hides the cards that do not match.
function boardHtml(shown, query, now) {
  const repoOf = new Map();
  for (const repo of shown) for (const pr of prsOf(repo)) repoOf.set(pr, repo);
  const all = [...repoOf.keys()];
  const cols = boardColumns(query ? all.filter(pr => matches(pr, query)) : all);
  const scale = findingsScale(all);
  const card = pr => {
    const repo = repoOf.get(pr);
    return cardHtml(repo.scope, pr, {
      me: repo.me, jiraUrl: repo.jira_url, repoLabel: repos.length > 1 ? repo.scope.split('/').pop() : '', scale, now,
    });
  };
  return `<div class="reviews-board">${COLUMNS.map(c => `<section class="reviews-col is-${c.key}" aria-label="${c.title}">
    <header class="reviews-col-head"><span class="reviews-col-dot"></span><h3>${c.title}</h3>
      <span class="reviews-col-count">${cols[c.key].length}</span></header>
    <div class="reviews-col-hint">${c.hint}</div>
    ${cols[c.key].length ? cols[c.key].map(card).join('')
    : `<div class="reviews-col-empty">${esc(query ? 'No PR matches' : c.empty)}</div>`}
  </section>`).join('')}</div>`;
}

// The open card and the j/k card as classes; a card that left the board keeps neither.
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
  view.querySelector('.reviews-top').hidden = !repos.length;
  redraw(view.querySelector('.reviews-status-lines'), repos.map(statusLineHtml).join(''));
  redraw(view.querySelector('.reviews-repos'), repoChipsHtml());
  const active = repoFilterOf(repoScope, repos);
  const shown = active ? repos.filter(r => r.scope === active) : repos;
  const now = Date.now();
  view.querySelector('.reviews-tiles').innerHTML = repos.length ? tilesHtml(boardTiles(shown.flatMap(prsOf), now)) : '';
  const query = view.querySelector('.reviews-search').value.trim().toLowerCase();
  redraw(view.querySelector('.reviews-list'), repos.length
    ? boardHtml(shown, query, now)
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
  // The card's language: the moves in words and the job line, never a turn code or a green "Report ready".
  const repo = repos.find(r => r.scope === target.scope);
  const move = moveText(pr, columnOf(pr), repo?.me);
  const flags = [pr.large && 'large', pr.draft && 'draft', pr.pipeline && 'pipeline'].filter(Boolean)
    .map(flag => `<span class="reviews-flag">${flag}</span>`).join('');
  return `<div class="review-panel-head">
    <div class="review-panel-row">${back}
      <a class="reviews-num" href="${esc(pr.url)}" target="_blank" rel="noopener"
        data-fk="num:panel:${esc(rowKey(target.scope, pr.number))}" aria-label="Open #${esc(pr.number)} on GitHub">#${esc(pr.number)}</a>
      ${ticketLinkHtml(pr.ticket_key, repo?.jira_url, 'panel', rowKey(target.scope, pr.number))}
      <span class="reviews-sub">${esc(pr.changed_files)} files</span>${flags}
    </div>
    <h3 class="review-panel-title" id="review-panel-title">${esc(cardTitle(pr.title, pr.ticket_key))}</h3>
    <div class="review-panel-row">${avatarsHtml(move.logins, pr.url)}<span class="review-panel-move">${esc(move.text)}</span>
      ${jobLineHtml(pr, Date.now())}<span class="review-spacer"></span>
      <div class="reviews-actions">${buttonsHtml(target.scope, pr, 'panel')}</div></div>
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

// posted / ticked / open as a word, never by its color alone; a number with no state is open.
function numberBadge(states, n) {
  const state = states && Object.hasOwn(NUMBER_STATE, states[n]) ? states[n] : 'open';
  return `<span class="review-badge is-${state}" title="${NUMBER_STATE[state]}"><span class="reviews-sr">Post state: </span>${
    state}</span>`;
}

function findingHtml(detail, item) {
  const kind = KIND_OF[item.kind];
  const where = item.path ? `${item.path}${item.line == null ? '' : `:${item.line}`}` : '';
  const meta = [where, item.note].filter(Boolean).map(esc).join(' · ');
  const body = itemBody(item.body);
  return details(`${detail.report_job}:${item.number}`, item.kind === 'red',
    `<span class="review-sq kind-${kind}" title="${KIND_NAME[kind]}"></span>
      <span class="review-n">${esc(item.number)}.</span><span class="review-t">${esc(item.title)}</span>
      ${numberBadge(detail.number_states, item.number)}${meta ? `<span class="review-where">${meta}</span>` : ''}`,
    body ? `<div class="review-body">${esc(body)}</div>` : '', 'review-finding');
}

// Delivery only: the run's other notes (a worktree left behind, …) are in the jobs table.
function telegramText(job) {
  if (job.message_sent) {
    const parts = job.message_ids.length;
    return `sent in ${parts} part${parts === 1 ? '' : 's'}`;
  }
  if (job.state !== 'done' && job.state !== 'posted') return `not sent: the job ended ${job.state}`;  // its PR moved on
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
    return items.length ? section(title, items.length, items.map(i => findingHtml(detail, i)).join('')) : '';
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
    <td>${esc(entry.n)}</td><td>${numberBadge(detail.reply_states, entry.n)}</td><td>${esc(replyTarget(entry))}</td>
    <td class="review-body">${esc(entry.body)}</td>
  </tr>`).join('');
  return section(title, null, `<div class="reviews-table-wrap"><table class="review-jobs">
    <thead><tr><th>N</th><th>State</th><th>Target</th><th>Body</th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

// A findings job's verdicts, shown under the report they fix (panelParts).
function verdictsSectionHtml(detail) {
  return section(`Fix of the findings <span>${detail.verdicts.length}</span> · job ${esc(detail.verdicts_job)}`, null,
    verdictsHtml(detail.verdicts));
}

// A fix job's fix (address or findings): its files, the protected ones, whether it was pushed, and the patch.
// Pushing is done by replying "push" in Telegram.
function fixHtml(detail) {
  const fix = detail.fix;
  if (!fix) return '';
  const rows = fix.files.map(f => `<tr><td><code>${esc(f.path)}</code></td><td>+${esc(f.added)}</td>
    <td>−${esc(f.deleted)}</td></tr>`).join('');
  const pushed = detail.pushed_sha
    ? `Pushed <code>${esc(detail.pushed_sha.slice(0, 9))}</code>` : 'Not pushed: reply "push" in Telegram.';
  const guarded = fix.protected.length
    ? `<div class="review-body">Protected files, never pushed: ${esc(fix.protected.join(', '))}</div>` : '';
  const patch = detail.fix_patch == null ? '' : details('patch', false, 'Patch',
    `<pre class="report-content">${esc(detail.fix_patch)}</pre>`, 'review-raw');
  return section('Fix', fix.files.length, `<div class="reviews-sub">${pushed} · not built or tested by Cleave</div>
    ${guarded}<div class="reviews-table-wrap"><table class="review-jobs">
    <thead><tr><th>File</th><th>Added</th><th>Deleted</th></tr></thead><tbody>${rows}</tbody></table></div>${patch}`);
}

// A link the post stored: an href only to GitHub, anything else as text; named by its anchor
// (pullrequestreview-…, discussion_r…).
function postLinkHtml(post, url, i) {
  const text = String(url);
  if (!text.startsWith(GITHUB)) return `<code class="review-url">${esc(text)}</code>`;
  const name = text.split('#')[1] || text.split('/').filter(Boolean).pop();
  return `<a class="reviews-num" href="${esc(text)}" target="_blank" rel="noopener noreferrer"
    data-fk="post:${esc(post.id)}:${i}" aria-label="Open ${esc(name)} on GitHub">${esc(name)} ↗</a>`;
}

// One post: its state, then kind · event · numbers · comments · head · time, its links and its error.
function postHtml(post) {
  const n = post.numbers.length;
  // fmtIso escapes a stamp it cannot parse.
  const meta = [
    esc(post.kind), esc(post.event),
    n ? `${n === 1 ? 'number' : 'numbers'} ${esc(post.numbers.join(', '))}` : '',
    `${esc(post.comments)} comment${post.comments === 1 ? '' : 's'}`,
    `at <code>${esc(post.head)}</code>`,
    `<time datetime="${esc(post.created_at)}" title="${fmtIso(post.created_at)}">${esc(timeAgo(post.created_at))}</time>`,
  ].filter(Boolean).join(' · ');
  const links = post.urls.map((url, i) => postLinkHtml(post, url, i)).join('');
  return `<li class="review-post">
    <div class="review-post-line">${stateDot(post.state, post.state)}<span class="reviews-sub">${meta}</span></div>
    ${links ? `<div class="review-post-links">${links}</div>` : ''}
    ${post.error ? `<div class="review-body">${esc(post.error)}</div>` : ''}
  </li>`;
}

// What Cleave posted from Telegram, newest first: the dashboard only shows it.
function postsHtml(posts) {
  if (!posts.length) {
    return section('Posts', null,
      '<div class="reviews-empty">Nothing posted yet. Posting is done by replying to the review in Telegram.</div>');
  }
  return section('Posts', posts.length, `<ul class="review-posts">${posts.map(postHtml).join('')}</ul>`);
}

// Why a PR has no report yet, from its newest job.
function noReportText(pr, jobs) {
  const job = jobs[0];
  if (!job) {
    if (pr && pr.large) return `No report: ${pr.changed_files} files, so no automatic review.`;
    return pr && pr.skipped ? 'No report: this head is skipped.' : 'No report yet.';
  }
  const what = { recheck: 're-check', address: 'run on your threads', findings: 'fix of the findings' }[job.kind]
    || 'review';
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
  // A re-check (or an address job, with its fix) newer than the report shows its replies instead of the findings;
  // a findings job's verdicts and fix go under the report they fix (panelParts).
  const parts = panelParts(detail);
  const fix = parts.fix ? fixHtml(detail) : '';
  const main = parts.view === 'replies' ? repliesHtml(detail) + fix
    : parts.view === 'report' ? reportHtml(detail) + (parts.verdicts ? verdictsSectionHtml(detail) : '') + fix
      : section('', null, `<div class="review-empty">${esc(noReportText(pr, detail.jobs))}</div>`);
  const raw = detail.report_text == null ? '' : section('', null, details('raw', false,
    `Raw report · job ${esc(detail.report_job)}`, `<pre class="report-content">${esc(detail.report_text)}</pre>`,
    'review-raw'));
  return main + postsHtml(detail.posts) + jobsHtml(detail.jobs) + raw;
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

// What a button queued: "a review", "a self-review", "a fix" or "a fix of the findings".
const queuedWhat = action => (action.startsWith('fix-findings') ? 'a fix of the findings'
  : action.startsWith('fix') ? 'a fix' : action.startsWith('self') ? 'a self-review' : 'a review');

// `inPanel` is asked at the click: a refresh while a confirmation is open replaces the button.
async function runAction(view, btn, inPanel) {
  const { reviewAction, scope, number } = btn.dataset;
  const skip = reviewAction === 'skip';
  const name = btn.textContent.trim().replace(/…$/, '');
  const pr = findPr({ scope, number: Number(number) });
  btn.disabled = true;
  btn.textContent = skip ? 'Skipping…' : 'Queuing…';
  let text, failed = false;
  try {
    const answer = await CALLS[reviewAction](scope, Number(number));
    text = skip ? `Skipped this head of #${number}${pr ? ` (${pr.head})` : ''}`
      : `Queued ${queuedWhat(reviewAction)} of #${number} at ${answer.job.head}`;
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
  const action = e.target.closest('[data-review-action]');
  if (action) {
    const kind = action.dataset.reviewAction;
    const inPanel = action.closest('.review-panel') !== null;
    const pr = findPr({ scope: action.dataset.scope, number: Number(action.dataset.number) });
    // It replaces a report or a fix of the same commit, or starts a Claude fix: ask first.
    if ((kind.endsWith('-again') || kind === 'fix-findings') && pr) {
      const ask = againConfirm(pr, kind);
      showConfirmDialog(ask.title, ask.bodyHtml, ask.label, () => runAction(view, action, inPanel));
      // The dialog takes the focus: Enter on the button behind it would open a second one.
      document.querySelector('.dialog-overlay #dlg-cancel')?.focus();
    } else {
      runAction(view, action, inPanel);  // a PR that left the board: the server answers why
    }
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
  openPr = null;
  focusKey = null;
  panelOpen = {};
  view.addEventListener('click', e => onClick(view, e));
  // An avatar's GitHub picture that does not load leaves its initials (an error does not bubble: capture it).
  view.addEventListener('error', e => {
    if (e.target instanceof HTMLImageElement && e.target.closest('.reviews-avatar')) e.target.remove();
  }, true);
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
