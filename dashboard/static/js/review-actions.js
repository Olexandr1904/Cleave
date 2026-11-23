// review-actions.js — the PR panel's push, post and command controls, as a pure model (no DOM): which buttons a
// panel's detail offers and the command each one sends to POST /api/reviews/action. It mirrors fix_buttons in
// orchestrator/reviewer/messages.py (the Telegram message's buttons), with `pushed` taken from `pushed_sha`.
// A button's `numbers: 'checked'` is filled with the ticked rows by resolveCommand at the click.

export const EVENTS = [
  ['COMMENT', 'Comment'], ['APPROVE', 'Approve'], ['REQUEST_CHANGES', 'Request changes'],
];

const isOpen = (states, n) => !states || states[n] !== 'posted';

function fixButtons(detail, withReplies) {
  const fix = detail.fix;
  if (!fix) return [];
  if (detail.pushed_sha) return withReplies ? [{ label: 'Reply all', command: { action: 'post_replies', all_replies: true } }] : [];
  const protectedFiles = fix.protected || [];
  if (protectedFiles.length) {
    return [{
      label: withReplies ? 'Push anyway + reply all…' : 'Push anyway…',
      command: { action: withReplies ? 'push_post' : 'push', anyway: true },
      confirm: `The fix touches protected files: ${protectedFiles.join(', ')}. Push it anyway?`,
    }];
  }
  return withReplies
    ? [{ label: 'Push + reply all', command: { action: 'push_post' } }, { label: 'Push', command: { action: 'push' } }]
    : [{ label: 'Push', command: { action: 'push' } }];
}

export function prActions(detail) {
  const replies = detail.replies || [];
  // A findings fix (its verdicts job) has no replies to post.
  const findings = Boolean(detail.fix_job) && detail.fix_job === detail.verdicts_job;
  const withReplies = !findings && replies.some(entry => isOpen(detail.reply_states, entry.n));
  const fix = fixButtons(detail, withReplies);
  const replyBar = [];
  if (withReplies) {
    if (!detail.fix) replyBar.push({ label: 'Reply all', command: { action: 'post_replies', all_replies: true } });
    replyBar.push({ label: 'Reply to checked', command: { action: 'post_replies', numbers: 'checked' } });
  }
  const states = detail.number_states || {};
  const numbers = detail.report ? detail.report.items.map(item => item.number) : Object.keys(states);
  const report = detail.report_job && numbers.some(n => isOpen(states, n)) ? [
    { label: 'Post all confirmed', command: { action: 'post', all_confirmed: true } },
    { label: 'Post checked', command: { action: 'post', numbers: 'checked' } },
  ] : [];
  const replyable = new Set([detail.report_job, detail.replies_job, detail.verdicts_job, detail.fix_job]);
  replyable.delete(null);
  replyable.delete(undefined);
  const newest = (detail.jobs || []).find(job => replyable.has(job.id));
  return { fix, replies: replyBar, report, commandJob: newest ? newest.id : null };
}

// The command to send: 'checked' becomes the ticked numbers (null when none is ticked), and a post carries the
// chosen review event.
export function resolveCommand(command, { numbers, event }) {
  const out = { ...command };
  if (out.numbers === 'checked') {
    if (!numbers.length) return null;
    out.numbers = [...numbers].sort((a, b) => a - b);
  }
  if (out.action === 'post') out.event = event;
  return out;
}
