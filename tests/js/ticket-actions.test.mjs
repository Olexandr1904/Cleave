// The ticket panel's controls (dashboard/static/js/ticket-actions.js): which controls a ticket gets.
// Run: node --test 'tests/js/*.test.mjs'
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { ticketControls, RETRY_STAGES, refusalOf, decisionBody, isDecided } =
  await import('../../dashboard/static/js/ticket-actions.js');

const ws = (state, fields = {}) => ({ ticket_id: 'ACME-123', current_state: state, pending_review_comments: null, ...fields });
const cm = (id, decision) => ({ comment_id: id, author: 'octo', file: 'app.py', line: 3, body: 'x', assessment: 'a', decision, hint_rounds: 0 });
const pick = c => ({ retry: c.retry, skip: c.skip, answer: c.answer, reviewed: c.reviewed });

test('a blocked ticket waiting for input gets the answer box, retry and skip', () => {
  assert.deepEqual(pick(ticketControls(ws('BLOCKED', { human_input_pending: true }))),
    { retry: true, skip: true, answer: true, reviewed: false });
});
test('a failed ticket gets retry and skip but no answer box', () => {
  assert.deepEqual(pick(ticketControls(ws('FAILED', { human_input_pending: true }))),
    { retry: true, skip: true, answer: false, reviewed: false });
});
test('a blocked ticket not waiting for input has no answer box', () => {
  assert.equal(ticketControls(ws('BLOCKED', { human_input_pending: false })).answer, false);
});
test('the stage list is the retry stages in pipeline order', () => {
  assert.deepEqual(ticketControls(ws('DEV')).stages, ['analysis', 'plan_review', 'dev', 'scope_check', 'code_review', 'qa', 'push']);
  assert.deepEqual(RETRY_STAGES, ticketControls(ws('DEV')).stages);
});
test('a PR_REVIEW ticket lists every escalated comment, decided or not, and offers reviewed', () => {
  const c = ticketControls(ws('PR_REVIEW', { pending_review_comments: [cm('1', null), cm('2', 'fix')] }));
  assert.deepEqual(c.comments.map(x => x.comment_id), ['1', '2']);
  assert.deepEqual(c.comments.map(isDecided), [false, true]);
  assert.equal(c.reviewed, true);
  assert.equal(c.retry, false);
});
test('comments of a ticket outside PR_REVIEW, or null comments, give an empty list', () => {
  assert.deepEqual(ticketControls(ws('DEV', { pending_review_comments: [cm('1', null)] })).comments, []);
  assert.deepEqual(ticketControls(ws('PR_REVIEW')).comments, []);
});
test('a refusal carries its message, a success none', () => {
  assert.equal(refusalOf({ status: 'error', message: 'X is DONE now: nothing was done.' }), 'X is DONE now: nothing was done.');
  assert.equal(refusalOf({ status: 'error' }), 'Refused.');
  assert.equal(refusalOf({ status: 'ok', new_state: 'DEV' }), null);
});
test('a decision body: reinvestigate needs a hint, wont_fix may have none', () => {
  assert.deepEqual(decisionBody('fix', ' '), { body: { decision: 'fix', text: '' } });
  assert.deepEqual(decisionBody('wont_fix', ' why '), { body: { decision: 'wont_fix', text: 'why' } });
  assert.deepEqual(decisionBody('wont_fix', ''), { body: { decision: 'wont_fix', text: '' } });
  assert.equal(decisionBody('reinvestigate', '  ').error, 'A hint is required to re-investigate.');
  assert.deepEqual(decisionBody('reinvestigate', 'look at x'), { body: { decision: 'reinvestigate', text: 'look at x' } });
});
test('a decided comment is listed with its decision; a hint round in progress is still undecided', () => {
  const c = ticketControls(ws('PR_REVIEW', { pending_review_comments: [cm('1', "won't fix: old"), { ...cm('2', null), hint_rounds: 1, pending_reinvestigation: true }] }));
  assert.equal(c.comments[0].decision, "won't fix: old");
  assert.equal(isDecided(c.comments[0]), true);
  assert.equal(isDecided(c.comments[1]), false);
});
