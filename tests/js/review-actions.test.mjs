// The PR panel's actions (dashboard/static/js/review-actions.js): which buttons, which commands.
// Run: node --test 'tests/js/*.test.mjs'
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { prActions, resolveCommand, mainCommand } = await import('../../dashboard/static/js/review-actions.js');

const r = n => ({ n, body: `reply ${n}` });
const detail = fields => ({
  report_job: null, replies_job: null, verdicts_job: null, fix_job: null, fix: null, pushed_sha: null,
  replies: [], reply_states: {}, number_states: {}, report: null, jobs: [], ...fields,
});
const labels = buttons => buttons.map(b => b.label);

test('a protected fix offers only push anyway, with a confirm', () => {
  const a = prActions(detail({ fix_job: 'f', fix: { protected: ['.github/x.yml'] }, replies: [r(1)] }));
  assert.deepEqual(labels(a.fix), ['Push anyway + reply all…']);
  assert.equal(a.fix[0].confirm.includes('.github/x.yml'), true);
  assert.deepEqual(a.fix[0].command, { action: 'push_post', anyway: true });
  const bare = prActions(detail({ fix_job: 'f', fix: { protected: ['a'] } }));
  assert.deepEqual(labels(bare.fix), ['Push anyway…']);
  assert.deepEqual(bare.fix[0].command, { action: 'push', anyway: true });
});

test('an unprotected fix offers push + reply all and push; without replies only push', () => {
  const a = prActions(detail({ fix_job: 'f', fix: {}, replies: [r(1)] }));
  assert.deepEqual(labels(a.fix), ['Push + reply all', 'Push']);
  assert.deepEqual(a.fix.map(b => b.command), [{ action: 'push_post' }, { action: 'push' }]);
  assert.deepEqual(labels(prActions(detail({ fix_job: 'f', fix: {} })).fix), ['Push']);
});

test('after the push: reply to open replies only', () => {
  const a = prActions(detail({ fix_job: 'f', fix: {}, pushed_sha: 'abc', replies: [r(1), r(2)], reply_states: { 1: 'posted', 2: 'open' } }));
  assert.deepEqual([...a.fix, ...a.replies].map(b => b.label), ['Reply all', 'Reply to checked']);
  const done = prActions(detail({ fix_job: 'f', fix: {}, pushed_sha: 'abc', replies: [r(1)], reply_states: { 1: 'posted' } }));
  assert.deepEqual([...done.fix, ...done.replies], []);
});

test('replies without a fix: reply all and reply to checked', () => {
  const a = prActions(detail({ replies_job: 'a', replies: [r(1), r(3)] }));
  assert.deepEqual(a.fix, []);
  assert.deepEqual(a.replies.map(b => b.command),
    [{ action: 'post_replies', all_replies: true }, { action: 'post_replies', numbers: 'checked' }]);
});

test('a findings fix has no replies to post', () => {
  const a = prActions(detail({ fix_job: 'v', verdicts_job: 'v', fix: {}, replies: [r(1)] }));
  assert.deepEqual([...a.fix, ...a.replies].map(b => b.label), ['Push']);
});

test('a report offers post all confirmed and post checked with the three events', () => {
  assert.deepEqual(prActions(detail({ report_job: 'r', number_states: { 1: 'open' } })).report.map(b => b.command),
    [{ action: 'post', all_confirmed: true }, { action: 'post', numbers: 'checked' }]);
  assert.deepEqual(prActions(detail({ report_job: 'r', number_states: { 1: 'posted' } })).report, []);
});

test('resolveCommand fills the ticked numbers and the event', () => {
  const post = { action: 'post', numbers: 'checked' };
  assert.deepEqual(resolveCommand(post, { numbers: [3, 1], event: 'APPROVE' }),
    { action: 'post', numbers: [1, 3], event: 'APPROVE' });
  assert.equal(resolveCommand(post, { numbers: [], event: 'COMMENT' }), null);  // nothing ticked: nothing to send
  assert.deepEqual(resolveCommand({ action: 'post', all_confirmed: true }, { numbers: [], event: 'COMMENT' }),
    { action: 'post', all_confirmed: true, event: 'COMMENT' });
  assert.deepEqual(resolveCommand({ action: 'push' }, { numbers: [1], event: 'APPROVE' }), { action: 'push' });
});

test('the command field addresses the newest job the operator could reply to', () => {
  const jobs = ['x', 'f', 'r', 'old'].map(id => ({ id }));
  assert.equal(prActions(detail({ jobs, report_job: 'r', fix_job: 'f' })).commandJob, 'f');
  assert.equal(prActions(detail({ jobs, report_job: 'r' })).commandJob, 'r');
  assert.equal(prActions(detail({ jobs })).commandJob, null);
});

test('mainCommand: push only, with the panel text when protected; reply all; post review with an event pick', () => {
  assert.deepEqual(mainCommand('push', detail({ fix_job: 'f', fix: { protected: [] }, replies: [r(1)] })),
    { jobId: 'f', command: { action: 'push' }, confirm: null, pickEvent: false });
  assert.deepEqual(mainCommand('push', detail({ fix_job: 'f', fix: { protected: ['a.yml', 'b.gradle'] } })),
    { jobId: 'f', command: { action: 'push', anyway: true },
      confirm: 'The fix touches protected files: a.yml, b.gradle. Push it anyway?', pickEvent: false });
  assert.deepEqual(mainCommand('replies', detail({ replies_job: 'a', replies: [r(1)] })),
    { jobId: 'a', command: { action: 'post_replies', all_replies: true }, confirm: null, pickEvent: false });
  assert.deepEqual(mainCommand('review', detail({ report_job: 'v' })),
    { jobId: 'v', command: { action: 'post', all_confirmed: true }, confirm: null, pickEvent: true });
});

test('mainCommand: a refusal when the PR has no job for the move', () => {
  assert.equal(mainCommand('push', detail({})), 'No fix to push.');
  assert.equal(mainCommand('replies', detail({})), 'No replies drafted.');
  assert.equal(mainCommand('review', detail({})), 'No report to post.');
});

test('the panel and the main button ask the same question before pushing protected files', () => {
  const d = detail({ fix_job: 'f', fix: { protected: ['a.yml'] } });
  assert.equal(prActions(d).fix[0].confirm, mainCommand('push', d).confirm);
});
