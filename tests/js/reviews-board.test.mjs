// The Reviews board's model (dashboard/static/js/reviews-board.js): columns, tiles, cards, buttons.
// Run: node --test 'tests/js/*.test.mjs'
process.env.TZ = 'UTC';

import { test } from 'node:test';
import assert from 'node:assert/strict';

const B = await import('../../dashboard/static/js/reviews-board.js');

const NOW = Date.parse('2026-11-19T12:00:00Z');
const H = 'aaaaaaaaa';
const ago = hours => new Date(NOW - hours * 3600e3).toISOString();

function pr(number, fields = {}) {
  return {
    number, title: `ACME-${number}: change ${number}`, url: `https://github.com/acme-org/acme-app/pull/${number}`,
    ticket_key: `ACME-${number}`, roles: ['reviewer'], my_turns: [], other_turns: [], pipeline: false,
    draft: false, changed_files: 3, head: H, job: null, last_change: ago(24), skipped: false, large: false,
    review: null, can_fix: false, fix: null, ...fields,
  };
}

function review(counts = {}, fields = {}) {
  return {
    id: 'r', state: 'done', head: H, started_at: ago(26), finished_at: ago(25), duration_s: 3610,
    cost_usd: 28.93, message_sent: true,
    findings: { critical: 0, major: 0, minor: 0, question: 0, preexisting: 0, dropped: 0, ...counts },
    ...fields,
  };
}

const other = (login, action) => ({ login, action });
const fixJob = (fields = {}) => ({ id: 'a1', state: 'done', head: H, started_at: ago(2), fix: true, pushed: false,
  posting: false, ...fields });

// Seven PRs shaped like a live board: three of my moves, four waiting on someone else.
const FIXTURE = [
  pr(11, { roles: ['author', 'assignee'], my_turns: ['merge'], last_change: ago(3) }),
  pr(12, { roles: ['author', 'reviewer'], ticket_key: '', title: 'CI: build messages',
    my_turns: ['resolve_conflicts', 'request_review'], review: review({ critical: 4, minor: 1, question: 2 }) }),
  pr(13, { roles: ['author', 'reviewer'], my_turns: ['request_review'], large: true, changed_files: 46,
    review: review({ critical: 15, minor: 3, question: 7 }), last_change: ago(48) }),
  pr(14, { roles: ['author'], other_turns: [other('octo-reviewer', 'recheck')], large: true, last_change: ago(49) }),
  pr(15, { other_turns: [other('octo-reviewer', 'answer_threads')], review: review({ question: 1 }, { state: 'posted' }) }),
  pr(16, { other_turns: [other('octo-reviewer', 'answer_threads')],
    review: review({ critical: 1, question: 1 }, { state: 'stale' }) }),
  pr(17, { other_turns: [other('octo-reviewer', 'merge')], last_change: ago(23) }),
];
const byNumber = n => FIXTURE.find(p => p.number === n);
const VIEW = { me: 'octo', jiraUrl: 'https://acme.atlassian.net', repoLabel: '', scale: 25, now: NOW };

test('columnOf: my review or re-check, then any other move of mine but merge, then merge; else With others', () => {
  const col = turns => B.columnOf(pr(1, { my_turns: turns }));
  assert.equal(col(['review']), 'review');
  assert.equal(col(['recheck', 'merge']), 'review');
  assert.equal(col(['resolve_conflicts', 'request_review']), 'fix');
  assert.equal(col(['merge', 'answer_threads']), 'fix');
  assert.equal(col(['brand_new_action']), 'fix');
  assert.equal(col(['merge']), 'merge');
  assert.equal(col([]), 'others');
  assert.equal(B.columnOf(pr(1, { other_turns: [other('octo-dev', 'merge')] })), 'others');
});

test('a PR I am not on goes to To review, marked, with whose move it really is', () => {
  const draft = pr(18, { roles: [], draft: true, other_turns: [other('octo-dev', 'finish_draft')] });
  assert.equal(B.columnOf(draft), 'review');
  assert.equal(B.columnOf(pr(19, { roles: [] })), 'review');
  assert.deepEqual(B.moveText(draft, 'review', 'octo'), { logins: ['octo-dev'], text: 'octo-dev: finish draft' });
  const card = B.cardHtml('acme/app', draft, VIEW);
  assert.match(card, /<article class="reviews-card is-not-involved"/);
  assert.match(card,
    /<span class="reviews-tag is-not-involved" title="You are not its author, an assignee or a reviewer">Not involved</);
  assert.match(card, /data-review-action="review-now"/);
  assert.doesNotMatch(card, /data-review-action="skip"/);  // Skip needs a review turn of mine
  assert.doesNotMatch(B.cardHtml('acme/app', byNumber(15), VIEW), /is-not-involved/);
});

test('the fixture lands in the four columns, oldest first, in board order', () => {
  assert.deepEqual(B.COLUMNS.map(c => c.key), ['review', 'fix', 'others', 'merge']);
  assert.deepEqual(B.COLUMNS.map(c => c.title), ['To review', 'To fix', 'With others', 'To merge']);
  const cols = B.boardColumns(FIXTURE);
  assert.deepEqual(Object.fromEntries(Object.entries(cols).map(([k, prs]) => [k, prs.map(p => p.number)])),
    { review: [], fix: [13, 12], others: [14, 15, 16, 17], merge: [11] });
});

test('cardTitle drops only a leading ticket key', () => {
  assert.equal(B.cardTitle('ACME-12: Area > Fix it', 'ACME-12'), 'Area > Fix it');
  assert.equal(B.cardTitle('ACME-12 - Fix', 'ACME-12'), 'Fix');
  assert.equal(B.cardTitle('Fix ACME-12', 'ACME-12'), 'Fix ACME-12');
  assert.equal(B.cardTitle('ACME-12', 'ACME-12'), 'ACME-12');
  assert.equal(B.cardTitle('ACME-123: other', 'ACME-12'), 'ACME-123: other');
  assert.equal(B.cardTitle('CI: x', ''), 'CI: x');
});

test('actionWords', () => {
  assert.equal(B.actionWords('recheck'), 're-check');
  assert.equal(B.actionWords('resolve_conflicts'), 'resolve conflicts');
  assert.equal(B.actionWords('brand_new_thing'), 'brand new thing');
});

test('reportAtHead: a done, posted or stale review of the current head', () => {
  for (const state of ['done', 'posted', 'stale']) assert.equal(B.reportAtHead(pr(1, { review: review({}, { state }) })), true);
  assert.equal(B.reportAtHead(pr(1, { review: review({}, { state: 'superseded' }) })), false);
  assert.equal(B.reportAtHead(pr(1, { review: review({}, { head: 'bbbbbbbbb' }) })), false);
  assert.equal(B.reportAtHead(pr(1)), false);
});

test('boardTiles on the fixture', () => {
  const t = B.boardTiles(FIXTURE, NOW);
  assert.deepEqual(t.needYou, { value: 3, sub: '2 to fix · 1 to merge', alert: false });
  assert.deepEqual(t.critical, { value: 19, sub: 'self-reviews: 15 in #13 · 4 in #12', alert: true });
  assert.deepEqual(t.waiting, { value: 4, sub: 'all on octo-reviewer', alert: false });
  assert.deepEqual(t.longest, { value: '2d', sub: '#14 · re-check', alert: false });
});

test('boardTiles: a PR I am not on is in To review but does not need me', () => {
  const notMine = [
    pr(18, { roles: [], draft: true, other_turns: [other('octo-dev', 'finish_draft')] }),
    pr(19, { roles: [], other_turns: [other('octo-dev', 'answer_threads')] }),
  ];
  assert.deepEqual(B.boardColumns([...FIXTURE, ...notMine]).review.map(p => p.number), [18, 19]);
  assert.deepEqual(B.boardTiles([...FIXTURE, ...notMine], NOW).needYou, { value: 3, sub: '2 to fix · 1 to merge', alert: false });
  const toReview = pr(20, { my_turns: ['review'] });
  assert.deepEqual(B.boardTiles([toReview, ...notMine], NOW).needYou, { value: 1, sub: '1 to review', alert: false });
});

test('boardTiles on an empty board', () => {
  const t = B.boardTiles([], NOW);
  assert.deepEqual([t.needYou.value, t.critical.value, t.waiting.value, t.longest.value], [0, 0, 0, '—']);
  assert.equal(t.critical.sub, 'no self-reviews');
  assert.equal(t.critical.alert, false);
});

test('boardTiles counts each login once per PR', () => {
  const prs = FIXTURE.map(p => p.number === 15
    ? { ...p, other_turns: [other('octo-reviewer', 'answer_threads'), other('octo-dev', 'review'), other('octo-dev', 'recheck')] }
    : p.number === 16 ? { ...p, other_turns: [other('octo-dev', 'review')] } : p);
  assert.equal(B.boardTiles(prs, NOW).waiting.sub, 'octo-reviewer 3 · octo-dev 2');
});

test('moveText: my actions in words, what is left to post, or who holds the move', () => {
  assert.deepEqual(B.moveText(byNumber(12), 'fix', 'octo'), { logins: ['octo'], text: 'resolve conflicts, request review' });
  assert.deepEqual(B.moveText(byNumber(14), 'others', 'octo'), { logins: ['octo-reviewer'], text: 'octo-reviewer: re-check' });
  const three = pr(1, { other_turns: [other('a1', 'review'), other('b2', 'review'), other('c3', 'merge')] });
  assert.equal(B.moveText(three, 'others', 'octo').logins.length, 3);
  assert.equal(B.moveText(pr(1), 'others', 'octo').text, "nobody's turn");
  const toPost = pr(1, { my_turns: ['review'], review: review({ critical: 1 }) });
  assert.equal(B.moveText(toPost, 'review', 'octo').text, 'post the review');
  // A card's line is one line: it names no channel (the confirm texts do).
  const recheck = pr(1, { my_turns: ['recheck'], job: { kind: 'recheck', state: 'done', head: H, message_sent: true } });
  assert.equal(B.moveText(recheck, 'review', 'octo').text, 'post the replies');
  const fixed = pr(1, { roles: ['author'], my_turns: ['answer_threads'], fix: fixJob() });
  assert.equal(B.moveText(fixed, 'fix', 'octo').text, 'fix ready: push and reply');
  assert.equal(B.moveText({ ...fixed, fix: fixJob({ pushed: true }) }, 'fix', 'octo').text,
    'fix pushed: post the replies');
  assert.equal(B.moveText({ ...fixed, fix: fixJob({ fix: false }) }, 'fix', 'octo').text,
    'replies ready: post');
  // A self-review queued after the fix is the row's job now: the fix still says what is left.
  assert.equal(B.moveText({ ...fixed, job: { kind: 'review', state: 'queued', head: H } }, 'fix', 'octo').text,
    'fix ready: push and reply');
  // A report not delivered yet cannot be posted from Telegram or here.
  const undelivered = pr(1, { my_turns: ['review'], review: review({ critical: 1 }, { message_sent: false }) });
  assert.equal(B.moveText(undelivered, 'review', 'octo').text, 'review');
});

test('cleaveMove: the move a card line names, by kind; none for GitHub turns alone', () => {
  const fixed = pr(1, { roles: ['author'], my_turns: ['answer_threads'], fix: fixJob() });
  assert.deepEqual(B.cleaveMove(fixed, 'fix'), { kind: 'push', text: 'fix ready: push and reply' });
  assert.deepEqual(B.cleaveMove({ ...fixed, fix: fixJob({ pushed: true }) }, 'fix'),
    { kind: 'replies', text: 'fix pushed: post the replies' });
  assert.deepEqual(B.cleaveMove({ ...fixed, fix: fixJob({ fix: false }) }, 'fix'),
    { kind: 'replies', text: 'replies ready: post' });
  assert.deepEqual(B.cleaveMove({ ...fixed, fix: fixJob({ kind: 'findings' }) }, 'fix'),
    { kind: 'push', text: 'fix ready: push' });  // a findings fix has no replies
  assert.equal(B.cleaveMove({ ...fixed, fix: fixJob({ kind: 'findings', pushed: true }) }, 'fix'), null);
  const recheck = pr(1, { my_turns: ['recheck'], job: { kind: 'recheck', state: 'done', head: H, message_sent: true } });
  assert.deepEqual(B.cleaveMove(recheck, 'review'), { kind: 'replies', text: 'post the replies' });
  const toPost = pr(1, { my_turns: ['review'], review: review({ critical: 1 }) });
  assert.deepEqual(B.cleaveMove(toPost, 'review'), { kind: 'review', text: 'post the review' });
  assert.equal(B.cleaveMove(byNumber(12), 'fix'), null);
});

test('mainButtonHtml: the move as a primary button; Push anyway… on protected files; none while posting', () => {
  const fixed = pr(1, { roles: ['author'], my_turns: ['answer_threads'], fix: fixJob() });
  const push = B.mainButtonHtml('acme/app', fixed, 'fix', 'card');
  assert.match(push, /class="reviews-btn primary is-main"[^>]*data-main-move="push"[^>]*>Push \+ reply all</);
  assert.match(push, /data-scope="acme\/app" data-number="1"/);
  assert.match(push, /data-fk="card:main:acme\/app#1"/);
  assert.match(B.mainButtonHtml('acme/app', { ...fixed, fix: fixJob({ protected: true }) }, 'fix', 'card'),
    /data-main-move="push"[^>]*>Push anyway \+ reply all…</);
  const findings = { ...fixed, fix: fixJob({ kind: 'findings' }) };  // no replies: the push alone
  assert.match(B.mainButtonHtml('acme/app', findings, 'fix', 'card'), /data-main-move="push"[^>]*>Push</);
  assert.match(B.mainButtonHtml('acme/app', { ...findings, fix: fixJob({ kind: 'findings', protected: true }) },
    'fix', 'card'), /data-main-move="push"[^>]*>Push anyway…</);
  assert.equal(B.mainButtonHtml('acme/app', { ...fixed, fix: fixJob({ posting: true }) }, 'fix', 'card'), '');
  assert.match(B.mainButtonHtml('acme/app', { ...fixed, fix: fixJob({ pushed: true }) }, 'fix', 'card'),
    /data-main-move="replies"[^>]*>Reply all</);
  const toPost = pr(1, { my_turns: ['review'], review: review({ critical: 1 }) });
  assert.match(B.mainButtonHtml('acme/app', toPost, 'review', 'panel'), /data-main-move="review"[^>]*>Post review…</);
  assert.match(B.mainButtonHtml('acme/app', toPost, 'review', 'panel'), /data-fk="panel:main:acme\/app#1"/);
  // resolve conflicts has no button: the next turn's does
  assert.match(B.mainButtonHtml('acme/app', byNumber(12), 'fix', 'card'), /data-main-move="request"[^>]*>Request review…</);
});

test('cardHtml: the main button comes before Fix again…, and its PR\'s last result shows on the card', () => {
  const fixed = pr(1, { roles: ['author'], my_turns: ['answer_threads'], can_fix: true, fix: fixJob() });
  const card = B.cardHtml('acme/app', fixed, VIEW);
  assert.ok(card.indexOf('data-main-move="push"') > 0);
  assert.ok(card.indexOf('data-main-move="push"') < card.indexOf('data-review-action="fix-again"'));
  const done = B.cardHtml('acme/app', fixed, { ...VIEW, result: { key: 'acme/app#1', state: 'done', text: 'Pushed abc.', card: true } });
  assert.match(done, /class="review-result is-done" role="status"><b>done<\/b>\s*<span>Pushed abc\.<\/span>/);
  const other = B.cardHtml('acme/app', fixed, { ...VIEW, result: { key: 'acme/app#2', state: 'done', text: 'x', card: true } });
  assert.doesNotMatch(other, /review-result/);
});

test('cardHtml: a PR whose action waits for its answer says so on its card', () => {
  const fixed = pr(1, { roles: ['author'], my_turns: ['answer_threads'], fix: fixJob() });
  const working = { key: 'acme/app#1', state: 'running', text: 'Working: the answer shows here.', card: true };
  assert.match(B.cardHtml('acme/app', fixed, { ...VIEW, result: working }),
    /class="review-result is-running" role="status"><b>running<\/b>\s*<span>Working: the answer shows here\.<\/span>/);
});

test('cardHtml: only a main button\'s result reaches the card, cut to its first line', () => {
  const fixed = pr(1, { roles: ['author'], my_turns: ['answer_threads'], fix: fixJob({ pushed: true }) });
  const fromPanel = { key: 'acme/app#1', state: 'shown', text: 'diff --git a/x b/x\n+one\n+two' };
  assert.doesNotMatch(B.cardHtml('acme/app', fixed, { ...VIEW, result: fromPanel }), /review-result/);
  const replies = { key: 'acme/app#1', state: 'posted', text: 'Posted 2 replies.\nhttps://x/1\nhttps://x/2', card: true };
  const card = B.cardHtml('acme/app', fixed, { ...VIEW, result: replies });
  assert.match(card, /<b>posted<\/b>\s*<span>Posted 2 replies\. …<\/span>/);
  assert.doesNotMatch(card, /https:\/\/x\/1/);
});

test('avatarsHtml: two avatars then +N; a GitHub picture over initials only on GitHub', () => {
  const three = B.avatarsHtml(['a1', 'b2', 'c3'], 'https://github.com/acme-org/acme-app/pull/1');
  assert.equal(three.split('class="reviews-avatar"').length - 1, 2);
  assert.match(three, /\+1/);
  const gitlab = B.avatarsHtml(['octo'], 'https://gitlab.com/x');
  assert.match(gitlab, />OC</);
  assert.doesNotMatch(gitlab, /<img/);
  const github = B.avatarsHtml(['octo'], 'https://github.com/acme-org/acme-app/pull/1');
  assert.match(github, /https:\/\/github\.com\/octo\.png\?size=40/);
  assert.doesNotMatch(github, /onerror/);
});

test('buttonsHtml: no button on my card unless Fix now applies; Self-review only in the panel', () => {
  assert.equal(B.buttonsHtml('acme/app', byNumber(12), 'card'), '');
  const fixable = { ...byNumber(12), can_fix: true };
  assert.match(B.buttonsHtml('acme/app', fixable, 'card'), /data-review-action="fix-now"[^>]*>Fix now</);
  const fixed = { ...fixable, fix: fixJob() };
  assert.match(B.buttonsHtml('acme/app', fixed, 'card'), /data-review-action="fix-again"[^>]*>Fix again…</);
  // A self-review newer than the fix does not turn Fix again… back into a plain Fix now.
  const reviewedSince = { ...fixed, job: { kind: 'review', state: 'queued', head: H } };
  assert.match(B.buttonsHtml('acme/app', reviewedSince, 'card'), /data-review-action="fix-again"/);
  const fixing = { ...fixable, fix: fixJob({ state: 'running' }), job: { kind: 'review', state: 'queued', head: H } };
  assert.match(B.buttonsHtml('acme/app', fixing, 'card'), /disabled[^>]*>Fixing</);
  const posting = { ...fixable, fix: fixJob({ posting: true }) };
  assert.match(B.buttonsHtml('acme/app', posting, 'card'), /disabled[^>]*>Posting</);
  assert.match(B.buttonsHtml('acme/app', byNumber(13), 'panel'), /data-review-action="self-review-again"[^>]*>Self-review again…</);
  assert.match(B.buttonsHtml('acme/app', byNumber(11), 'panel'), /data-review-action="self-review"[^>]*>Self-review</);
});

test('buttonsHtml on a PR I review', () => {
  assert.match(B.buttonsHtml('acme/app', byNumber(15), 'card'), /data-review-action="review-again"[^>]*>Review again…</);
  assert.match(B.buttonsHtml('acme/app', pr(18, { large: true }), 'card'), /data-review-action="review-now"[^>]*>Review anyway</);
  assert.match(B.buttonsHtml('acme/app', pr(18), 'card'), />Review now</);
  assert.match(B.buttonsHtml('acme/app', pr(18, { my_turns: ['review'] }), 'card'), /data-review-action="skip"/);
  assert.match(B.buttonsHtml('acme/app', pr(19, { review: review({}, { state: 'queued' }) }), 'card'), /disabled[^>]*>Queued</);
  for (const p of [...FIXTURE, pr(18)]) {
    for (const where of ['card', 'panel']) assert.doesNotMatch(B.buttonsHtml('acme/app', p, where), /GitHub/);
  }
});

test('againConfirm names the last run before replacing its report or fix', () => {
  const p = { ...byNumber(13), review: review({}, { finished_at: '2026-11-18T10:34:00Z', duration_s: 3610, cost_usd: 28.93 }) };
  const c = B.againConfirm(p, 'self-review-again');
  assert.equal(c.title, 'Run a self-review of #13 again at the same commit?');
  for (const part of ['18 Nov 10:34', '1h 0m', '$28.93']) assert.ok(c.bodyHtml.includes(part), part);
  assert.equal(c.label, 'Run self-review');
  const free = B.againConfirm({ ...p, review: { ...p.review, cost_usd: null } }, 'self-review-again');
  assert.ok(!free.bodyHtml.includes('$'));
  const other15 = B.againConfirm(byNumber(15), 'review-again');
  assert.equal(other15.title, 'Review #15 again at the same commit?');
  assert.equal(other15.label, 'Review again');
  const fix = B.againConfirm({ ...byNumber(12), fix: fixJob() }, 'fix-again');
  assert.equal(fix.title, 'Run the fix of #12 again?');
  assert.ok(fix.bodyHtml.includes('push it first (here or in Telegram)'));
  assert.equal(fix.label, 'Run again');
  const pushed = B.againConfirm({ ...byNumber(12), fix: fixJob({ pushed: true }) }, 'fix-again');
  assert.ok(pushed.bodyHtml.includes('was pushed') && !pushed.bodyHtml.includes('push it first (here or in Telegram)'));
});

test('shortAge: minutes, hours, days; old from 48 hours', () => {
  assert.deepEqual(B.shortAge(ago(0.5), NOW), { text: '30m', old: false });
  assert.deepEqual(B.shortAge(ago(3), NOW), { text: '3h', old: false });
  assert.deepEqual(B.shortAge(ago(47), NOW), { text: '1d', old: false });
  assert.deepEqual(B.shortAge(ago(48), NOW), { text: '2d', old: true });
});

test('findingsHtml: severity in words on a shared scale; nothing without a done, posted or stale review', () => {
  const html = B.findingsHtml(byNumber(13), 25);
  for (const part of ['15 critical', '3 minor', '7 questions', 'self-review · 18 Nov 10:00', 'report in Telegram']) {
    assert.ok(html.includes(part), part);
  }
  assert.ok(!html.includes('pre-existing'));
  assert.ok(B.findingsHtml(byNumber(15), 25).includes('posted'));
  assert.ok(!B.findingsHtml(byNumber(15), 25).includes('self-review'));
  assert.equal(B.findingsHtml(byNumber(11), 25), '');   // no review
  assert.equal(B.findingsHtml(pr(1, { review: review({ critical: 1 }, { state: 'superseded' }) }), 25), '');
  const older = { ...byNumber(13), review: { ...byNumber(13).review, head: 'bbbbbbbbb' } };
  assert.ok(B.findingsHtml(older, 25).includes('on an older commit'));
});

test('findingsHtml: a report your turn left (stale) still shows its findings, dimmed, and says turn gone', () => {
  const html = B.findingsHtml(byNumber(16), 25);
  for (const part of ['1 critical', '1 question', 'turn gone', 'reviews-findings is-stale']) assert.ok(html.includes(part), part);
  assert.ok(!html.includes('report in Telegram'));  // Telegram refuses to post from a stale report
  assert.ok(!html.includes('on an older commit'));
  const older = { ...byNumber(16), review: { ...byNumber(16).review, head: 'bbbbbbbbb' } };
  assert.ok(B.findingsHtml(older, 25).includes('turn gone · on an older commit'));
});

test('jobLineHtml: only while something is happening', () => {
  assert.equal(B.jobLineHtml(byNumber(11), NOW), '');
  const running = pr(1, { job: { kind: 'review', state: 'running', started_at: ago(0.2), head: H } });
  assert.ok(B.jobLineHtml(running, NOW).includes('Cleave review running · 12 min'));
  const fixing = pr(1, { roles: ['author'], job: { kind: 'address', state: 'running', started_at: ago(0.2), head: H } });
  assert.ok(B.jobLineHtml(fixing, NOW).includes('Claude fixing · 12 min'));
  // A fix still running under a newer self-review: both lines.
  const both = pr(1, { roles: ['author'], job: { id: 'r2', kind: 'review', state: 'queued', head: H },
    fix: fixJob({ state: 'running', started_at: ago(0.2) }) });
  const html = B.jobLineHtml(both, NOW);
  assert.ok(html.includes('Cleave review queued') && html.includes('Claude fixing · 12 min'), html);
  const failed = pr(1, { job: { kind: 'review', state: 'failed', reason: 'boom', head: H } });
  assert.ok(B.jobLineHtml(failed, NOW).includes('failed: boom'));
  assert.ok(B.jobLineHtml(pr(1, { skipped: true }), NOW).includes('skipped'));
});

test('cardHtml: links to GitHub and Jira, the title without its key, every value escaped', () => {
  const html = B.cardHtml('acme/app', byNumber(11), VIEW);
  assert.ok(html.includes('href="https://github.com/acme-org/acme-app/pull/11"'));
  assert.ok(html.includes('href="https://acme.atlassian.net/browse/ACME-11"'));
  assert.ok(html.includes('>change 11<'));
  assert.ok(html.includes('data-review-row="acme/app#11"'));
  const plain = B.cardHtml('acme/app', byNumber(11), { ...VIEW, jiraUrl: '' });
  assert.ok(!plain.includes('atlassian') && plain.includes('ACME-11'));
  // Two PRs of one ticket (a skill-flow comparison): their ticket links keep distinct focus keys.
  const twin = B.cardHtml('acme/app', pr(21, { ticket_key: 'ACME-11' }), VIEW);
  const fk = h => h.match(/class="reviews-ticket"[^>]*data-fk="([^"]+)"/)[1];
  assert.notEqual(fk(html), fk(twin));
  const evil = B.cardHtml('acme/app', pr(20, { title: '<script>x</script>', ticket_key: '' }), VIEW);
  assert.ok(evil.includes('&lt;script&gt;') && !evil.includes('<script>'));
});

test('repoFilterOf: a listed scope filters; an unknown or no scope shows every repo', () => {
  const repos = [{ scope: 'acme/app' }, { scope: 'acme/web' }];
  assert.equal(B.repoFilterOf('acme/app', repos), 'acme/app');
  assert.equal(B.repoFilterOf('old/repo', repos), null);
  assert.equal(B.repoFilterOf(null, repos), null);
});

// --- Findings jobs: Fix findings… ---

const mineNoTurn = (fields = {}) => pr(1, { roles: ['author'], ...fields });

test('a fix ready to push puts my PR in To fix, of either kind', () => {
  const ready = mineNoTurn({ fix: fixJob({ kind: 'findings' }) });
  assert.equal(B.columnOf(ready), 'fix');
  assert.equal(B.moveText(ready, 'fix', 'octo').text, 'fix ready: push');
  assert.equal(B.columnOf({ ...ready, fix: fixJob() }), 'fix');                          // an address fix
  assert.equal(B.columnOf({ ...ready, fix: fixJob({ kind: 'findings', pushed: true }) }), 'others');
  assert.equal(B.columnOf({ ...ready, fix: fixJob({ kind: 'findings', fix: false }) }), 'others');
  assert.equal(B.columnOf({ ...ready, fix: fixJob({ kind: 'findings', state: 'queued' }) }), 'others');
  assert.equal(B.columnOf({ ...ready, my_turns: ['merge'] }), 'fix');                     // the push before the merge
  assert.equal(B.columnOf({ ...ready, my_turns: ['merge'], fix: null }), 'merge');
  assert.equal(B.columnOf({ ...ready, my_turns: ['recheck'] }), 'review');
});

test('the move line of a findings fix: only while it waits for the push', () => {
  const turn = mineNoTurn({ my_turns: ['request_review'] });
  for (const fields of [{ pushed: true }, { fix: false }, { state: 'failed' }]) {
    assert.equal(B.moveText({ ...turn, fix: fixJob({ kind: 'findings', ...fields }) }, 'fix', 'octo').text,
      'request review', JSON.stringify(fields));
  }
  // An address fix keeps its lines.
  assert.equal(B.moveText({ ...turn, fix: fixJob({ fix: false }) }, 'fix', 'octo').text, 'replies ready: post');
});

test('Fix findings… in the panel only, with its states', () => {
  const base = mineNoTurn({ can_fix_findings: true });
  assert.match(B.buttonsHtml('acme/app', base, 'panel'), /data-review-action="fix-findings"[^>]*>Fix findings…</);
  assert.doesNotMatch(B.buttonsHtml('acme/app', base, 'card'), /fix-findings/);
  assert.doesNotMatch(B.buttonsHtml('acme/app', { ...base, can_fix_findings: false }, 'panel'), /fix-findings/);
  for (const state of ['done', 'posted']) {
    const again = { ...base, fix: fixJob({ kind: 'findings', state }) };
    assert.match(B.buttonsHtml('acme/app', again, 'panel'), /data-review-action="fix-findings-again"[^>]*>Fix findings again…</);
  }
  const failed = { ...base, fix: fixJob({ kind: 'findings', state: 'failed' }) };
  assert.match(B.buttonsHtml('acme/app', failed, 'panel'), />Fix findings…</);
  const addressDone = { ...base, fix: fixJob({ kind: 'address' }) };
  assert.match(B.buttonsHtml('acme/app', addressDone, 'panel'), /data-review-action="fix-findings"[^>]*>Fix findings…</);
  assert.equal(B.fixState(addressDone, 'findings'), 'none');
  assert.equal(B.fixState(addressDone), 'again');
  for (const [state, label] of [['queued', 'Queued'], ['running', 'Running'], ['deferred', 'Deferred']]) {
    const pending = { ...base, fix: fixJob({ kind: 'address', state }) };
    assert.match(B.buttonsHtml('acme/app', pending, 'panel'), new RegExp(`disabled[^>]*>${label}<`), state);
    assert.doesNotMatch(B.buttonsHtml('acme/app', pending, 'panel'), /fix-findings/);
  }
  const posting = { ...base, fix: fixJob({ kind: 'findings', posting: true }) };
  assert.match(B.buttonsHtml('acme/app', posting, 'panel'), /disabled[^>]*>Posting</);
  // With Fix now too: one disabled button, not two.
  const both = { ...base, can_fix: true, fix: fixJob({ state: 'queued' }) };
  assert.equal((B.buttonsHtml('acme/app', both, 'panel').match(/>Queued</g) || []).length, 1);
  const bothPosting = { ...base, can_fix: true, fix: fixJob({ posting: true }) };
  assert.equal((B.buttonsHtml('acme/app', bothPosting, 'panel').match(/>Posting</g) || []).length, 1);
  // A findings fix done: Fix now stays Fix now (it does not replace that fix), Fix findings again… replaces it.
  const findingsDone = { ...base, can_fix: true, fix: fixJob({ kind: 'findings' }) };
  const html = B.buttonsHtml('acme/app', findingsDone, 'panel');
  assert.match(html, /data-review-action="fix-now"/);
  assert.doesNotMatch(html, /data-review-action="fix-again"/);
  assert.match(html, /data-review-action="fix-findings-again"/);
});

test('a pushed findings fix is not offered again: the next one is a plain Fix findings…', () => {
  // self-review → Fix findings… → "push" → a self-review at the new head: nothing unpushed is replaced.
  const pushed = mineNoTurn({ can_fix_findings: true, fix: fixJob({ kind: 'findings', pushed: true }) });
  assert.equal(B.fixState(pushed, 'findings'), 'none');
  const html = B.buttonsHtml('acme/app', pushed, 'panel');
  assert.match(html, /data-review-action="fix-findings"[^>]*>Fix findings…</);
  assert.doesNotMatch(html, /fix-findings-again/);
});

test('Fix findings… asks first, naming the confirmed findings and the head', () => {
  const p = mineNoTurn({ review: review({ critical: 1, major: 2, minor: 1, question: 3, preexisting: 2 }, { head: '8f01a2b3c' }) });
  const c = B.againConfirm(p, 'fix-findings');
  assert.equal(c.title, 'Fix the findings of the self-review?');
  assert.equal(c.label, 'Fix findings');
  assert.equal(c.bodyHtml, 'Claude (Opus) weighs each of the 4 confirmed findings of the self-review at 8f01a2b3c, '
    + 'fixes those that hold and says why for the rest. Nothing is pushed until you push it, here or in Telegram.');
  const one = B.againConfirm(mineNoTurn({ review: review({ minor: 1 }, { head: '8f01a2b3c' }) }), 'fix-findings');
  assert.match(one.bodyHtml, /weighs the 1 confirmed finding of the self-review at 8f01a2b3c, fixes it if it holds and says why if not\./);
  const again = B.againConfirm({ ...p, fix: fixJob({ kind: 'findings', started_at: '2026-11-19T10:00:00Z' }) },
    'fix-findings-again');
  assert.equal(again.title, 'Fix the findings of the self-review?');
  assert.ok(again.bodyHtml.endsWith(' or in Telegram. The fix from 19 Nov 10:00 is replaced; it was not pushed.'), again.bodyHtml);
  const noTime = B.againConfirm({ ...p, fix: fixJob({ kind: 'findings', started_at: '' }) }, 'fix-findings-again');
  assert.ok(noTime.bodyHtml.endsWith(' The current fix is replaced; it was not pushed.'), noTime.bodyHtml);
  const hostile = B.againConfirm(mineNoTurn({ review: review({ critical: 1 }, { head: '<b>' }) }), 'fix-findings');
  assert.doesNotMatch(hostile.bodyHtml, /<b>/);
});

test('panelParts: each fix beside the job it belongs to, verdicts under the report they fix', () => {
  const V = [{ n: 1, verdict: 'FIXED', why: 'x' }];
  // jobs are newest first; R a self-review, A an address job, F a findings job.
  const detail = (ids, fields) => ({ jobs: ids.map(id => ({ id })), report_job: null, replies_job: null,
    verdicts_job: null, verdicts: [], verdicts_review: null, fix_job: null, ...fields });
  // R → A → F at one head: F's verdicts and F's fix under R's report (A's replies are older).
  assert.deepEqual(B.panelParts(detail(['F', 'A', 'R'], { report_job: 'R', replies_job: 'A', verdicts_job: 'F',
    verdicts: V, verdicts_review: 'R', fix_job: 'F' })), { view: 'report', verdicts: true, fix: true });
  // R → F → A: A's replies with A's own fix; F's verdicts are older.
  assert.deepEqual(B.panelParts(detail(['A', 'F', 'R'], { report_job: 'R', replies_job: 'A', verdicts_job: 'F',
    verdicts: V, verdicts_review: 'R', fix_job: 'A' })), { view: 'replies', verdicts: false, fix: true });
  // The replies view never shows another job's fix.
  assert.deepEqual(B.panelParts(detail(['A', 'F', 'R'], { report_job: 'R', replies_job: 'A', fix_job: 'F' })),
    { view: 'replies', verdicts: false, fix: false });
  // R1 → F (pushed) → R2: R2's report, without R1's verdicts or F's fix.
  assert.deepEqual(B.panelParts(detail(['R2', 'F', 'R1'], { report_job: 'R2', verdicts_job: 'F', verdicts: V,
    verdicts_review: 'R1', fix_job: 'F' })), { view: 'report', verdicts: false, fix: false });
  // A report newer than the address job: the report, no fix (as before findings jobs).
  assert.deepEqual(B.panelParts(detail(['R', 'A'], { report_job: 'R', replies_job: 'A', fix_job: 'A' })),
    { view: 'report', verdicts: false, fix: false });
  assert.deepEqual(B.panelParts(detail(['A'], { replies_job: 'A', fix_job: 'A' })),
    { view: 'replies', verdicts: false, fix: true });
  assert.equal(B.panelParts(detail([], {})).view, 'none');
});

test('verdictsHtml lists each verdict, escaped', () => {
  const html = B.verdictsHtml([{ n: 2, verdict: 'FIXED', why: 'null <check> added' }, { n: 3, verdict: "WON'T FIX", why: 'intended' }]);
  assert.match(html, /<li[^>]*>2\. FIXED — null &lt;check&gt; added<\/li>/);
  assert.match(html, /<li[^>]*>3\. WON'T FIX — intended<\/li>/);
  assert.equal(B.verdictsHtml([]), '');
});


test('turnMove: the first of my turns with a button, when Cleave\'s own move names none', () => {
  const mine = fields => pr(1, { roles: ['author'], ...fields });
  assert.deepEqual(B.turnMove(pr(1, { my_turns: ['review'] }), 'review'), { kind: 'review-now' });
  assert.equal(B.turnMove(pr(1, { my_turns: ['recheck'] }), 'review'), null);  // the re-check job runs on its own
  assert.deepEqual(B.turnMove(mine({ my_turns: ['answer_threads'], can_fix: true }), 'fix'), { kind: 'fix' });
  assert.equal(B.turnMove(mine({ my_turns: ['address_review'] }), 'fix'), null);  // Fix now does not apply
  assert.deepEqual(B.turnMove(mine({ my_turns: ['address_review', 'merge'] }), 'fix'), { kind: 'merge' });
  assert.deepEqual(B.turnMove(mine({ my_turns: ['request_review'], rerequest: ['rev-a', 'rev-b'] }), 'fix'),
    { kind: 'rerequest', logins: ['rev-a', 'rev-b'] });
  assert.deepEqual(B.turnMove(mine({ my_turns: ['request_review'], rerequest: [] }), 'fix'), { kind: 'request' });
  assert.deepEqual(B.turnMove(mine({ my_turns: ['request_review'] }), 'fix'), { kind: 'request' });
  assert.deepEqual(B.turnMove(mine({ my_turns: ['finish_draft'] }), 'fix'), { kind: 'ready' });
  assert.deepEqual(B.turnMove(mine({ my_turns: ['merge'] }), 'merge'), { kind: 'merge' });
  assert.equal(B.turnMove(mine({ my_turns: ['resolve_conflicts', 'fix_checks'] }), 'fix'), null);
  assert.equal(B.turnMove(pr(1, { roles: [], my_turns: [] }), 'review'), null);  // not involved
  // Cleave's own move comes first: a fix to push is the line's move
  assert.equal(B.turnMove(mine({ my_turns: ['answer_threads'], can_fix: true, fix: fixJob() }), 'fix'), null);
});

test('mainButtonHtml: a GitHub move as a main button; Review now and Fix now are the card\'s own', () => {
  const mine = fields => pr(7, { roles: ['author'], ...fields });
  const html = (p, column = 'fix') => B.mainButtonHtml('acme/app', p, column, 'card');
  assert.match(html(mine({ my_turns: ['request_review'], rerequest: ['rev-a', 'rev-b'] })),
    /class="reviews-btn primary is-main" data-main-move="rerequest"[^>]*>Re-request rev-a, rev-b</);
  assert.match(html(mine({ my_turns: ['request_review'] })), /data-main-move="request"[^>]*>Request review…</);
  assert.match(html(mine({ my_turns: ['finish_draft'] })), /data-main-move="ready"[^>]*>Ready for review</);
  assert.match(html(mine({ my_turns: ['merge'] }), 'merge'), /data-main-move="merge"[^>]*>Merge…</);
  assert.match(html(mine({ my_turns: ['merge'] }), 'merge'), /data-fk="card:main:acme\/app#7"/);
  // a fix being posted holds Cleave's moves only
  assert.match(html(mine({ my_turns: ['merge'], fix: fixJob({ posting: true, pushed: true, state: 'posted' }) }), 'merge'),
    /data-main-move="merge"/);
  assert.equal(html(pr(7, { my_turns: ['review'] }), 'review'), '');
  assert.equal(html(mine({ my_turns: ['answer_threads'], can_fix: true })), '');
  assert.match(B.mainButtonHtml('acme/<x>', mine({ my_turns: ['request_review'], rerequest: ['<b>'] }), 'fix', 'card'),
    /data-scope="acme\/&lt;x&gt;"[^>]*>Re-request &lt;b&gt;</);
});

test('buttonsHtml: Review now and Fix now are marked main when the line names them', () => {
  const toReview = pr(3, { my_turns: ['review'] });
  assert.match(B.buttonsHtml('acme/app', toReview, 'card'), /class="reviews-btn primary is-main"\s+data-review-action="review-now"/);
  const queued = pr(3, { my_turns: ['review'], review: review({}, { state: 'queued' }) });
  assert.match(B.buttonsHtml('acme/app', queued, 'card'), /class="reviews-btn is-main" disabled[^>]*>Queued</);
  const notMine = pr(3, { roles: [], my_turns: [] });  // not involved: Review now shows on hover only
  assert.doesNotMatch(B.buttonsHtml('acme/app', notMine, 'card'), /is-main/);
  const fixable = pr(4, { roles: ['author'], my_turns: ['answer_threads'], can_fix: true });
  assert.match(B.buttonsHtml('acme/app', fixable, 'card'), /class="reviews-btn primary is-main"\s+data-review-action="fix-now"/);
  // Review again… re-runs a paid review: it stays a hover button
  const reviewed = pr(3, { my_turns: ['review'], review: review({}, { state: 'done', message_sent: false }) });
  assert.match(B.buttonsHtml('acme/app', reviewed, 'card'), /data-review-action="review-again"/);
  assert.doesNotMatch(B.buttonsHtml('acme/app', reviewed, 'card'), /is-main/);
  const merge = pr(4, { roles: ['author'], my_turns: ['merge'], can_fix: true });
  assert.doesNotMatch(B.buttonsHtml('acme/app', merge, 'card'), /is-main/);  // the line names Merge…
});

test('pickedReviewers: ticked first, then typed ones split on commas and spaces, @ dropped, each once', () => {
  assert.deepEqual(B.pickedReviewers(['rev-a'], ' @rev-b, rev-c  rev-a,,'), ['rev-a', 'rev-b', 'rev-c']);
  assert.deepEqual(B.pickedReviewers([], ''), []);
  assert.deepEqual(B.pickedReviewers([], undefined), []);
  assert.deepEqual(B.pickedReviewers(['Rev-A'], 'rev-a REV-B rev-b'), ['Rev-A', 'REV-B']);  // logins ignore case
});

test('candidatesHtml and mergeHtml: the move dialogs\' bodies', () => {
  const pick = B.candidatesHtml([{ login: 'rev-a', reason: 'suggested by GitHub' }, { login: '<x>', reason: 'r & d' }]);
  assert.match(pick, /<input type="checkbox" name="reviewer" value="rev-a">/);
  assert.match(pick, /value="&lt;x&gt;"> &lt;x&gt; <span class="reviews-sub">r &amp; d<\/span>/);
  assert.match(pick, /name="reviewer-other"/);
  assert.doesNotMatch(pick, /checked/);
  assert.match(B.candidatesHtml([]), /GitHub suggests nobody/);
  const merge = B.mergeHtml(7, { methods: ['merge', 'squash'], default: 'squash' });
  assert.match(merge, /#7/);
  assert.match(merge, /value="squash" checked> Squash and merge/);
  assert.match(merge, /value="merge"> Create a merge commit/);
  assert.doesNotMatch(merge, /rebase/);
});

test('a merge GitHub blocks while no check ran offers Re-run CI on its card and panel', () => {
  const blocked = pr(7, { roles: ['author'], my_turns: ['merge'], no_checks: true });
  assert.equal(B.columnOf(blocked), 'merge');
  assert.deepEqual(B.turnMove(blocked, 'merge'), { kind: 'rerun' });
  assert.deepEqual(B.turnMove({ ...blocked, my_turns: ['address_review', 'merge'] }, 'fix'), { kind: 'rerun' });
  assert.deepEqual(B.moveText(blocked, 'merge', 'octo'), { logins: ['octo'], text: 'merge: no checks ran, re-run CI' });
  assert.match(B.mainButtonHtml('acme/app', blocked, 'merge', 'card'), /data-main-move="rerun"[^>]*>Re-run CI</);
  assert.match(B.mainButtonHtml('acme/app', blocked, 'merge', 'panel'), /data-fk="panel:main:acme\/app#7"[^>]*>Re-run CI</);
  assert.deepEqual(B.turnMove({ ...blocked, no_checks: false }, 'merge'), { kind: 'merge' });
  assert.equal(B.moveText({ ...blocked, no_checks: false }, 'merge', 'octo').text, 'merge');
});
