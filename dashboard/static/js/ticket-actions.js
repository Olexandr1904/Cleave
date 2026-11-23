// ticket-actions.js — pure: which controls the ticket panel offers (no DOM)

export const RETRY_STAGES = ['analysis', 'plan_review', 'dev', 'scope_check', 'code_review', 'qa', 'push'];

export function ticketControls(ws) {
  const state = ws.current_state;
  const stopped = state === 'BLOCKED' || state === 'FAILED';
  const prReview = state === 'PR_REVIEW';
  const comments = prReview && Array.isArray(ws.pending_review_comments)
    ? ws.pending_review_comments
    : [];
  return {
    retry: stopped,
    stages: RETRY_STAGES,
    skip: stopped,
    answer: state === 'BLOCKED' && !!ws.human_input_pending,
    comments,
    reviewed: prReview,
  };
}

// postJSON returns a 400 refusal instead of throwing; its message is why nothing was done.
export function refusalOf(result) {
  if (result && result.status === 'error') return result.message || 'Refused.';
  return null;
}

export function decisionBody(decision, text) {
  const t = (decision === 'fix' ? '' : (text || '')).trim();
  if (decision === 'reinvestigate' && !t) return { error: 'A hint is required to re-investigate.' };
  return { body: { decision, text: t } };
}

// A comment is decided once it has a decision; a hint round in progress leaves it undecided.
export function isDecided(c) {
  return !!c.decision;
}
