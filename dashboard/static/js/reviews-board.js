// reviews-board.js — the Reviews board's model: which column a PR is in, the four tiles, a card, its buttons and
// the confirmation before re-running a review or a fix. Pure functions of the /api/reviews rows: no DOM, so Node
// can test them (tests/js/reviews-board.test.mjs). reviews.js draws the board and handles its events.

import { esc } from './helpers.js';

// Left to right. A PR is in one column: my review or re-check, else any other move of mine but merge or a fix ready
// to push (the push is my move), else my merge; no move of mine, With others.
export const COLUMNS = [
  { key: 'review', title: 'To review', hint: 'your review or re-check', empty: 'Nothing to review' },
  { key: 'fix', title: 'To fix', hint: 'your move on your PR', empty: 'Nothing to fix' },
  { key: 'others', title: 'With others', hint: 'no move of yours', empty: "Nobody else's move" },
  { key: 'merge', title: 'To merge', hint: 'your merge', empty: 'Nothing to merge' },
];

const WORDS = {
  review: 'review', recheck: 're-check', merge: 'merge', resolve_conflicts: 'resolve conflicts',
  request_review: 'request review', answer_threads: 'answer threads', resolve_threads: 'resolve threads',
  address_review: 'address review', finish_draft: 'finish draft', fix_checks: 'fix checks',
};
const PENDING = ['queued', 'running', 'deferred'];
const REPORTED = ['done', 'posted', 'stale'];  // a report of its head exists: Review now would supersede it
const SHOWN = ['done', 'posted'];  // a report the board shows
// The finding kinds the card counts, in severity order: [key, class, word for 1, word for more].
const KINDS = [
  ['critical', 'c', 'critical', 'critical'], ['major', 'mj', 'major', 'major'],
  ['minor', 'mn', 'minor', 'minor'], ['question', 'q', 'question', 'questions'],
];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export const actionWords = action => WORDS[action] ?? String(action).replaceAll('_', ' ');

// The repo the hash names (#/reviews/<scope>) when it is listed; else null: every repo.
export const repoFilterOf = (scope, repos) => (scope && repos.some(r => r.scope === scope) ? scope : null);
export const isMine = pr => pr.roles.includes('author');

// The PR's newest fix (of either kind) is done, made a patch and is not pushed: pushing it from Telegram is my move.
const readyFix = pr => Boolean(pr.fix && pr.fix.state === 'done' && pr.fix.fix && !pr.fix.pushed);

export function columnOf(pr) {
  const turns = pr.my_turns;
  if (turns.some(a => a === 'review' || a === 'recheck')) return 'review';
  if (turns.some(a => a !== 'merge') || readyFix(pr)) return 'fix';
  return turns.length ? 'merge' : 'others';
}

const ms = iso => Date.parse(iso);

// Each column's PRs, the longest unchanged first.
export function boardColumns(prs) {
  const cols = Object.fromEntries(COLUMNS.map(c => [c.key, []]));
  for (const pr of prs) cols[columnOf(pr)].push(pr);
  for (const key of Object.keys(cols)) cols[key].sort((a, b) => (ms(a.last_change) || 0) - (ms(b.last_change) || 0));
  return cols;
}

// The title as the PR has it, without a leading ticket key: the card's header shows the key.
export function cardTitle(title, ticketKey) {
  const text = String(title ?? '');
  if (!ticketKey || !text.startsWith(ticketKey)) return text;
  const rest = text.slice(ticketKey.length);
  if (rest && !/^[\s:\-–]/.test(rest)) return text;  // ACME-123 is not ACME-12
  return rest.replace(/^\s*[:\-–]?\s*/, '') || text;
}

export function reportAtHead(pr) {
  return Boolean(pr.review) && pr.review.head === pr.head && REPORTED.includes(pr.review.state);
}

// The pending state of a review of the current head, or ''.
export function reviewPending(pr) {
  const r = pr.review;
  return r && r.head === pr.head && PENDING.includes(r.state) ? r.state : '';
}

// From the PR's newest fix job of either kind (pr.fix), whatever job is newer: a self-review after a fix must not hide
// it. Posting and pending hold both buttons (one fix of a PR at a time); "again" is the button's own `kind` only: Fix
// now does not replace a findings fix, nor Fix findings… a threads fix.
export function fixState(pr, kind = 'address') {
  const fix = pr.fix;
  if (!fix) return 'none';
  if (fix.posting) return 'posting';  // a push or a post of its replies is under way: the server refuses a new fix
  if (PENDING.includes(fix.state)) return 'pending';
  return REPORTED.includes(fix.state) && (fix.kind || 'address') === kind ? 'again' : 'none';
}

export function shortAge(iso, now) {
  const t = ms(iso);
  if (isNaN(t)) return { text: '', old: false };
  const hours = Math.max(0, now - t) / 3600e3;
  const text = hours < 1 ? `${Math.floor(hours * 60)}m` : hours < 24 ? `${Math.floor(hours)}h` : `${Math.floor(hours / 24)}d`;
  return { text, old: hours >= 48 };
}

const pad = n => String(n).padStart(2, '0');

export function fmtClock(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtWhen(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : `${d.getDate()} ${MONTHS[d.getMonth()]} ${fmtClock(iso)}`;
}

export function fmtDuration(seconds) {
  if (seconds == null) return '';
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return seconds >= 60 ? `${Math.floor(seconds / 60)}m` : `${seconds}s`;
}

const shownReview = pr => pr.review && SHOWN.includes(pr.review.state) && pr.review.findings ? pr.review : null;
const counted = f => f.critical + f.major + f.minor + f.question;

// The largest number of counted findings on the board: the bars share this scale.
export function findingsScale(prs) {
  return Math.max(1, ...prs.map(shownReview).filter(Boolean).map(r => counted(r.findings)));
}

export function boardTiles(prs, now) {
  const cols = boardColumns(prs);
  const mineCount = ['review', 'fix', 'merge'].reduce((n, k) => n + cols[k].length, 0);
  const needSub = [['review', 'to review'], ['fix', 'to fix'], ['merge', 'to merge']]
    .filter(([k]) => cols[k].length).map(([k, w]) => `${cols[k].length} ${w}`).join(' · ');

  const selfReviews = prs.filter(pr => isMine(pr) && shownReview(pr) && pr.review.head === pr.head);
  const critical = selfReviews.map(pr => [pr.number, pr.review.findings.critical]).filter(([, c]) => c > 0)
    .sort((a, b) => b[1] - a[1]);
  const total = critical.reduce((n, [, c]) => n + c, 0);
  const listed = critical.slice(0, 2).map(([n, c]) => `${c} in #${n}`).join(' · ') + (critical.length > 2 ? ' · …' : '');
  const critSub = !selfReviews.length ? 'no self-reviews' : critical.length ? `self-reviews: ${listed}` : 'none in self-reviews';

  const waiting = cols.others;
  const tally = new Map();
  for (const pr of waiting) for (const login of new Set(pr.other_turns.map(t => t.login))) tally.set(login, (tally.get(login) || 0) + 1);
  const holders = [...tally].sort((a, b) => b[1] - a[1]);
  const waitSub = holders.length === 1 && holders[0][1] === waiting.length ? `all on ${holders[0][0]}`
    : holders.map(([login, n]) => `${login} ${n}`).join(' · ');

  const oldest = waiting[0];  // boardColumns sorts the longest unchanged first
  const longest = oldest
    ? { value: shortAge(oldest.last_change, now).text,
      sub: [`#${oldest.number}`, ...oldest.other_turns.slice(0, 1).map(t => actionWords(t.action))].join(' · ') }
    : { value: '—', sub: '' };
  return {
    needYou: { value: mineCount, sub: needSub, alert: false },
    critical: { value: total, sub: critSub, alert: total > 0 },
    waiting: { value: waiting.length, sub: waitSub, alert: false },
    longest: { ...longest, alert: false },
  };
}

// What the card's bottom line says and whose avatars it shows.
export function moveText(pr, column, me) {
  if (column === 'others') {
    const logins = [...new Set(pr.other_turns.map(t => t.login))];
    if (!logins.length) return { logins, text: "nobody's turn" };
    const of = login => [...new Set(pr.other_turns.filter(t => t.login === login).map(t => actionWords(t.action)))];
    return { logins, text: logins.map(login => `${login}: ${of(login).join(', ')}`).join('; ') };
  }
  const job = pr.job;
  const fix = pr.fix;
  let text = [...new Set(pr.my_turns.map(actionWords))].join(', ');
  // Posting is done from the Telegram message: only once it was delivered.
  if (column === 'review' && job && job.kind === 'recheck' && job.state === 'done' && job.message_sent) {
    text = 'post the replies from Telegram';
  } else if (column === 'review' && pr.review && pr.review.state === 'done' && pr.review.head === pr.head
    && pr.review.message_sent) {
    text = 'post the review from Telegram';
  } else if (column === 'fix' && fix && fix.state === 'done' && fix.kind === 'findings') {
    if (readyFix(pr)) text = 'fix ready: push from Telegram';  // pushed or no patch: nothing left to do with it
  } else if (column === 'fix' && fix && fix.state === 'done') {
    text = fix.pushed ? 'fix pushed: post the replies from Telegram'
      : fix.fix ? 'fix ready: push from Telegram' : 'replies ready: post from Telegram';
  }
  return { logins: me ? [me] : [], text };
}

function hue(login) {
  let h = 0;
  for (const ch of login) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

// At most two avatars, then +N. On GitHub a picture covers the initials; reviews.js removes one that fails.
export function avatarsHtml(logins, prUrl) {
  const github = String(prUrl ?? '').startsWith('https://github.com/');
  const shown = logins.slice(0, 2).map(login => `<span class="reviews-avatar" title="${esc(login)}"
    style="--av-hue:${hue(login)}">${esc(login.slice(0, 2).toUpperCase())}${github
    ? `<img src="https://github.com/${esc(encodeURIComponent(login))}.png?size=40" alt="" loading="lazy">` : ''}</span>`);
  const more = logins.length > 2 ? `<span class="reviews-avatar-more">+${logins.length - 2}</span>` : '';
  return `<span class="reviews-avatars">${shown.join('')}${more}</span>`;
}

// `id` names the PR in the focus key: the two PRs of a skill-flow comparison share a ticket.
export function ticketLinkHtml(key, jiraUrl, where = 'row', id = key) {
  if (!key) return '';
  if (!jiraUrl) return `<span class="reviews-ticket">${esc(key)}</span>`;
  return `<a class="reviews-ticket" href="${esc(jiraUrl)}/browse/${esc(encodeURIComponent(key))}" target="_blank"
    rel="noopener" data-fk="tk:${esc(where)}:${esc(id)}">${esc(key)}</a>`;
}

export function findingsHtml(pr, scale) {
  const r = shownReview(pr);
  if (!r) return '';
  const f = r.findings;
  const total = counted(f);
  const older = r.head !== pr.head;
  const kinds = KINDS.filter(([key]) => f[key]);
  const words = kinds.map(([key, cls, one, many]) => `<span class="sev-${cls}">${f[key]} ${f[key] === 1 ? one : many}</span>`);
  const label = kinds.map(([key, , one, many]) => `${f[key]} ${f[key] === 1 ? one : many}`).join(', ') || 'no findings';
  const bar = total ? `<div class="reviews-sbar" role="img" aria-label="${esc(label)}" style="width:${
    Math.max(4, Math.round(total / scale * 100))}%">${kinds.map(([key, cls]) => `<i class="sev-${cls}" style="flex:${f[key]}"></i>`).join('')}</div>` : '';
  const when = fmtWhen(r.started_at);
  const source = isMine(pr) ? `<div class="reviews-src">self-review${when ? ` · ${esc(when)}` : ''}</div>` : '';
  const state = r.state === 'posted' ? 'posted' : r.message_sent ? 'report in Telegram' : 'not delivered yet';
  return `<div class="reviews-findings${older ? ' is-older' : ''}">${source}${bar}<div class="reviews-sev">${
    words.join('') || '<span>no findings</span>'}<span class="reviews-sev-x">· ${state}${older ? ' · on an older commit' : ''}</span></div></div>`;
}

// A findings fix's verdicts in the PR panel: `n. VERDICT — why` each (model text, escaped); '' without any.
export function verdictsHtml(verdicts) {
  if (!verdicts.length) return '';
  // The Dropped list's look: a number, then the text.
  return `<ul class="review-dropped">${verdicts.map(v => `<li>${esc(v.n)}. ${esc(v.verdict)} — ${esc(v.why)}</li>`)
    .join('')}</ul>`;
}

const JOB_NAME = { review: 'Cleave review', recheck: 'Cleave re-check', address: 'Claude fix' };

const jobLine = (state, text) => `<div class="reviews-jobline is-${state}"><span class="reviews-dot"></span>${esc(text)}</div>`;

// One job's line while it is queued, running, deferred or failed; '' otherwise.
function oneJobLine(job, now) {
  if (!job || ![...PENDING, 'failed'].includes(job.state)) return '';
  const name = JOB_NAME[job.kind] || job.kind;
  if (job.state === 'running') {
    const t = ms(job.started_at);
    const min = isNaN(t) ? '' : ` · ${Math.max(0, Math.floor((now - t) / 60000))} min`;
    return jobLine('running', `${job.kind === 'address' ? 'Claude fixing' : `${name} running`}${min}`);
  }
  if (job.state === 'deferred') {
    return jobLine('deferred', `${name} deferred${job.retry_at ? ` · next try ${fmtClock(job.retry_at)}` : ''}`);
  }
  if (job.state === 'failed') return jobLine('failed', `${name} failed${job.reason ? `: ${job.reason}` : ''}`);
  return jobLine('queued', `${name} queued`);
}

// The job lines: the row's job, and the newest fix when it is pending under a newer job; else skipped or nothing.
export function jobLineHtml(pr, now) {
  const fix = pr.fix && PENDING.includes(pr.fix.state) && pr.fix.id !== pr.job?.id ? { ...pr.fix, kind: 'address' } : null;
  const lines = oneJobLine(pr.job, now) + oneJobLine(fix, now);
  return lines || (pr.skipped ? jobLine('skipped', 'skipped') : '');
}

const PENDING_LABEL = { review: { queued: 'Queued', running: 'Running', deferred: 'Deferred' },
  fix: { queued: 'Queued', running: 'Fixing', deferred: 'Deferred' },
  findings: { queued: 'Queued', running: 'Running', deferred: 'Deferred' } };

// The buttons of a card ('card') or of the panel header ('panel'). Nothing here opens GitHub: #number does.
export function buttonsHtml(scope, pr, where) {
  const key = `${scope}#${pr.number}`;
  const target = `data-scope="${esc(scope)}" data-number="${esc(pr.number)}"`;
  const button = (action, label, primary = false) => `<button class="reviews-btn${primary ? ' primary' : ''}"
    data-review-action="${action}" ${target} data-fk="${where}:${action}:${esc(key)}">${esc(label)}</button>`;
  const disabled = (label, title) => `<button class="reviews-btn" disabled title="${esc(title)}">${esc(label)}</button>`;
  const mine = isMine(pr);
  const out = [];
  if (!mine || where === 'panel') {
    const pending = reviewPending(pr);
    if (pending) {
      const retry = pr.job && pr.job.id === pr.review.id && pr.job.retry_at;
      const title = pending !== 'deferred' ? `A review of this head is ${pending}`
        : retry ? `Deferred; retries at ${fmtClock(retry)}` : 'Deferred; retries automatically';
      out.push(disabled(PENDING_LABEL.review[pending], title));
    } else if (reportAtHead(pr)) {
      out.push(mine ? button('self-review-again', 'Self-review again…') : button('review-again', 'Review again…'));
    } else {
      out.push(mine ? button('self-review', 'Self-review', true)
        : button('review-now', pr.large ? 'Review anyway' : 'Review now', true));
    }
  }
  if (mine && pr.can_fix) {
    const state = fixState(pr);
    if (state === 'posting') out.push(disabled('Posting', 'A push or a post of this fix is under way'));
    else if (state === 'pending') out.push(disabled(PENDING_LABEL.fix[pr.fix.state], `A fix of this PR is ${pr.fix.state}`));
    else if (state === 'again') out.push(button('fix-again', 'Fix again…'));
    else out.push(button('fix-now', 'Fix now', true));
  }
  if (mine && where === 'panel' && pr.can_fix_findings) {
    const state = fixState(pr, 'findings');
    const held = state === 'posting' || state === 'pending';
    // A posting or pending fix holds both buttons: its disabled button is shown once, by Fix now when it is there.
    if (held && !pr.can_fix) {
      out.push(state === 'posting' ? disabled('Posting', 'A push or a post of a fix of this PR is under way')
        : disabled(PENDING_LABEL.findings[pr.fix.state], `A fix of this PR is ${pr.fix.state}`));
    } else if (state === 'again') out.push(button('fix-findings-again', 'Fix findings again…'));
    else if (!held) out.push(button('fix-findings', 'Fix findings…'));
  }
  // Skip drops a queued or deferred review of this head too (Review now after a skip).
  const pending = reviewPending(pr);
  if (!mine && pr.my_turns.includes('review') && (!pr.skipped || pending === 'queued' || pending === 'deferred')) {
    out.push(button('skip', 'Skip'));
  }
  return out.join('');
}

function andList(parts) {
  return parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : parts.join('');
}

// The confirmation before a run that replaces a report or a fix of the same commit, and before Fix findings….
export function againConfirm(pr, action) {
  if (action === 'fix-findings' || action === 'fix-findings-again') {
    const r = pr.review || {};
    const f = r.findings || {};
    const n = (f.critical || 0) + (f.major || 0) + (f.minor || 0);
    const what = n === 1
      ? `weighs the 1 confirmed finding of the self-review at ${r.head}, fixes it if it holds and says why if not.`
      : `weighs each of the ${n} confirmed findings of the self-review at ${r.head}, fixes those that hold and says why for the rest.`;
    let body = `Claude (Opus) ${what} Nothing is pushed until you reply "push" in Telegram.`;
    if (action === 'fix-findings-again') {
      const when = fmtWhen((pr.fix || {}).started_at);
      body += ` ${when ? `The fix from ${when}` : 'The current fix'} is replaced; it was not pushed.`;
    }
    return { title: 'Fix the findings of the self-review?', label: 'Fix findings', bodyHtml: esc(body) };
  }
  if (action === 'fix-again') {
    const fix = pr.fix || {};
    const what = fmtWhen(fix.started_at) ? `The fix from ${fmtWhen(fix.started_at)}` : 'The current fix';
    return { title: `Run the fix of #${pr.number} again?`, label: 'Run again',
      bodyHtml: esc(fix.pushed
        ? `${what} was pushed; a new run replaces its drafted replies, so post them from Telegram first if you want them.`
        : `${what} is replaced; push it from Telegram first if you want it.`) };
  }
  const self = action === 'self-review-again';
  const r = pr.review || {};
  const parts = [
    r.finished_at && fmtWhen(r.finished_at) && `finished ${fmtWhen(r.finished_at)}`,
    r.duration_s != null && `took ${fmtDuration(r.duration_s)}`,
    r.cost_usd != null && `cost $${Number(r.cost_usd).toFixed(2)} (API-equivalent)`,
  ].filter(Boolean);
  const body = parts.length ? `The last one ${andList(parts)}. The new report replaces it.`
    : 'The new report replaces the current one.';
  return {
    title: self ? `Run a self-review of #${pr.number} again at the same commit?` : `Review #${pr.number} again at the same commit?`,
    bodyHtml: esc(body), label: self ? 'Run self-review' : 'Review again',
  };
}

// One card. `view`: {me, jiraUrl, repoLabel (the repo's name with several repos, else ''), scale, now}.
export function cardHtml(scope, pr, view) {
  const key = `${scope}#${pr.number}`;
  const column = columnOf(pr);
  const move = moveText(pr, column, view.me);
  const age = shortAge(pr.last_change, view.now);
  const flags = [pr.large && 'large', pr.draft && 'draft', pr.pipeline && 'pipeline'].filter(Boolean)
    .map(flag => `<span class="reviews-flag">${flag}</span>`).join('');
  const title = cardTitle(pr.title, pr.ticket_key);
  const buttons = buttonsHtml(scope, pr, 'card');
  return `<article class="reviews-card" tabindex="0" data-review-row="${esc(key)}" data-fk="row:${esc(key)}"
      data-scope="${esc(scope)}" data-number="${esc(pr.number)}">
    <div class="reviews-card-head"><span class="reviews-tag${isMine(pr) ? ' is-mine' : ''}">${isMine(pr) ? 'Your PR' : 'Reviewing'}</span>
      <a class="reviews-num" href="${esc(pr.url)}" target="_blank" rel="noopener" data-fk="num:row:${esc(key)}"
        aria-label="Open #${esc(pr.number)} on GitHub">#${esc(pr.number)}</a>${ticketLinkHtml(pr.ticket_key, view.jiraUrl, 'row', key)}${flags}${
      view.repoLabel ? `<span class="reviews-repo-label">${esc(view.repoLabel)}</span>` : ''}</div>
    <div class="reviews-card-title" title="${esc(pr.title)}">${esc(title)}</div>
    ${findingsHtml(pr, view.scale)}${jobLineHtml(pr, view.now)}
    <div class="reviews-card-foot">${avatarsHtml(move.logins, pr.url)}<span class="reviews-move">${esc(move.text)}</span>
      <span class="reviews-age${age.old ? ' is-old' : ''}">${esc(age.text)}</span></div>
    ${buttons ? `<div class="reviews-actions">${buttons}</div>` : ''}
  </article>`;
}
