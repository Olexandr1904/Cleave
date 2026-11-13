// reviews.js — Reviews tab: per repo, the PRs where it is my turn and the ones waiting on others.
// Read-only plus Review now, Skip and the reviews switches: nothing is posted from the dashboard.

import { loadReviews, reviewNow, skipReview } from './api.js';
import { renderSwitchPanel } from './switches.js';
import { esc, timeAgo } from './helpers.js';

const JOB_BADGE = {
  queued: 'badge-blue', running: 'badge-purple', done: 'badge-green', posted: 'badge-green',
  failed: 'badge-red', deferred: 'badge-yellow',
};

function badge(text, cls) {
  return `<span class="state-badge ${esc(cls)}">${esc(text)}</span>`;
}

function reasonText(repo) {
  if (repo.status === 'ok' || !repo.reason) return '';
  return repo.status === 'doc_missing' ? `reviews are off: ${repo.reason}` : repo.reason;
}

function turnsHtml(pr) {
  const mine = pr.my_turns.map(action => `<strong>${esc(action)}</strong>`);
  const others = pr.other_turns.map(t => `${esc(t.login)}: ${esc(t.action)}`);
  return [...mine, ...others].join(', ');
}

function rowHtml(scope, pr) {
  const target = `data-scope="${esc(scope)}" data-number="${esc(pr.number)}"`;
  // A skipped head can still have a queued review (Review now after Skip): Skip drops it.
  const queuedHere = pr.job && pr.job.kind === 'review' && pr.job.state === 'queued'
    && pr.job.head === pr.head;
  const skip = pr.my_turns.includes('review') && (!pr.skipped || queuedHere)
    ? `<button data-review-action="skip" ${target} title="Skip this head">Skip</button>` : '';
  const job = pr.job ? badge(pr.job.state, JOB_BADGE[pr.job.state] || 'badge-gray')
    : pr.skipped ? badge('skipped', 'badge-gray') : '';
  return `<tr>
    <td><a href="${esc(pr.url)}" target="_blank" rel="noopener">#${esc(pr.number)}</a></td>
    <td>${esc(pr.title)}${pr.pipeline ? ` ${badge('pipeline', 'badge-gray')}` : ''}</td>
    <td>${esc(pr.ticket_key)}</td>
    <td>${esc(pr.roles.join(', '))}</td>
    <td>${turnsHtml(pr)}</td>
    <td>${job}</td>
    <td>${esc(timeAgo(pr.last_change))}</td>
    <td class="reviews-actions"><button data-review-action="review-now" ${target}>Review now</button>${skip}</td>
  </tr>`;
}

function tableHtml(title, scope, prs) {
  const body = prs.length
    ? `<table class="reviews-table">
        <thead><tr>
          <th>PR</th><th>Title</th><th>Ticket</th><th>Roles</th><th>Turns</th><th>Job</th><th>Last change</th><th></th>
        </tr></thead>
        <tbody>${prs.map(pr => rowHtml(scope, pr)).join('')}</tbody>
      </table>`
    : '<div class="reviews-empty">None.</div>';
  return `<div class="reviews-group-title">${esc(title)}</div>${body}`;
}

function repoHtml(repo) {
  const reason = reasonText(repo);
  return `<div class="project-group">
    <div class="project-group-title">${esc(repo.scope)} · ${esc(repo.status)}</div>
    ${reason ? `<div class="reviews-reason">${esc(reason)}</div>` : ''}
    ${tableHtml('My turn', repo.scope, repo.my_turn)}
    ${tableHtml('Waiting on others', repo.scope, repo.waiting)}
  </div>`;
}

function bindActions(content) {
  content.querySelectorAll('[data-review-action]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const { reviewAction, scope, number } = btn.dataset;
      const call = reviewAction === 'skip' ? skipReview : reviewNow;
      btn.disabled = true;
      try {
        await call(scope, Number(number));
      } catch (err) {
        alert(`${btn.textContent} failed: ${err.message}`);
      }
      await renderReviews();
    });
  });
}

export async function renderReviews() {
  const content = document.getElementById('content');
  let repos;
  try {
    ({ repos } = await loadReviews());
  } catch (e) {
    content.innerHTML = `<div class="state-msg" style="color:#f85149;">Error: ${esc(String(e))}</div>`;
    return;
  }
  const body = repos.length
    ? repos.map(repoHtml).join('')
    : '<div class="state-msg">No repo has reviews enabled.</div>';
  content.innerHTML = `<div id="switch-panel-container" class="reviews-switches"></div>${body}`;
  renderSwitchPanel(document.getElementById('switch-panel-container'), 'reviews');
  bindActions(content);
}
