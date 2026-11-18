// The Reviews board's model (dashboard/static/js/reviews-board.js): columns, tiles, cards, buttons.
// Run: node --test tests/js/
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
    review: null, can_fix: false, ...fields,
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
  assert.equal(B.moveText(toPost, 'review', 'octo').text, 'post the review from Telegram');
  const fixed = pr(1, { roles: ['author'], my_turns: ['answer_threads'], job: { kind: 'address', state: 'done', fix: true } });
  assert.equal(B.moveText(fixed, 'fix', 'octo').text, 'fix ready: push from Telegram');
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
  const fixed = { ...fixable, job: { kind: 'address', state: 'done', head: H } };
  assert.match(B.buttonsHtml('acme/app', fixed, 'card'), /data-review-action="fix-again"[^>]*>Fix again…</);
  const fixing = { ...fixable, job: { kind: 'address', state: 'running', head: H } };
  assert.match(B.buttonsHtml('acme/app', fixing, 'card'), /disabled[^>]*>Fixing</);
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
  const fix = B.againConfirm({ ...byNumber(12), job: { kind: 'address', state: 'done', started_at: ago(2) } }, 'fix-again');
  assert.equal(fix.title, 'Run the fix of #12 again?');
  assert.ok(fix.bodyHtml.includes('push it from Telegram first'));
  assert.equal(fix.label, 'Run again');
});

test('shortAge: minutes, hours, days; old from 48 hours', () => {
  assert.deepEqual(B.shortAge(ago(0.5), NOW), { text: '30m', old: false });
  assert.deepEqual(B.shortAge(ago(3), NOW), { text: '3h', old: false });
  assert.deepEqual(B.shortAge(ago(47), NOW), { text: '1d', old: false });
  assert.deepEqual(B.shortAge(ago(48), NOW), { text: '2d', old: true });
});

test('findingsHtml: severity in words on a shared scale; nothing without a done or posted review', () => {
  const html = B.findingsHtml(byNumber(13), 25);
  for (const part of ['15 critical', '3 minor', '7 questions', 'self-review · 18 Nov 10:00', 'report in Telegram']) {
    assert.ok(html.includes(part), part);
  }
  assert.ok(!html.includes('pre-existing'));
  assert.ok(B.findingsHtml(byNumber(15), 25).includes('posted'));
  assert.ok(!B.findingsHtml(byNumber(15), 25).includes('self-review'));
  assert.equal(B.findingsHtml(byNumber(16), 25), '');   // stale
  assert.equal(B.findingsHtml(byNumber(11), 25), '');   // no review
  const older = { ...byNumber(13), review: { ...byNumber(13).review, head: 'bbbbbbbbb' } };
  assert.ok(B.findingsHtml(older, 25).includes('on an older commit'));
});

test('jobLineHtml: only while something is happening', () => {
  assert.equal(B.jobLineHtml(byNumber(11), NOW), '');
  const running = pr(1, { job: { kind: 'review', state: 'running', started_at: ago(0.2), head: H } });
  assert.ok(B.jobLineHtml(running, NOW).includes('Cleave review running · 12 min'));
  const fixing = pr(1, { roles: ['author'], job: { kind: 'address', state: 'running', started_at: ago(0.2), head: H } });
  assert.ok(B.jobLineHtml(fixing, NOW).includes('Claude fixing · 12 min'));
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
  const evil = B.cardHtml('acme/app', pr(20, { title: '<script>x</script>', ticket_key: '' }), VIEW);
  assert.ok(evil.includes('&lt;script&gt;') && !evil.includes('<script>'));
});

test('repoFilterOf: a listed scope filters; an unknown or no scope shows every repo', () => {
  const repos = [{ scope: 'acme/app' }, { scope: 'acme/web' }];
  assert.equal(B.repoFilterOf('acme/app', repos), 'acme/app');
  assert.equal(B.repoFilterOf('old/repo', repos), null);
  assert.equal(B.repoFilterOf(null, repos), null);
});
