# tests/unit/test_reviewer_jobs.py
from __future__ import annotations
import dataclasses
from datetime import datetime, timezone
from config.schemas import ProcessPartConfig, ReviewerConfig
from orchestrator.reviewer.board import BoardUpdate, TurnChange
from orchestrator.reviewer.jobs import Notice, held_prs, plan_jobs
from orchestrator.reviewer.lines import doc_missing_line, poll_failed_line, readiness_failed_line
from orchestrator.reviewer.models import BoardRow, Job

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
OLD, RECENT = "2026-11-20T11:00:00+00:00", "2026-11-20T11:50:00+00:00"
A, B = "a" * 40, "b" * 40
CFG = ReviewerConfig(enabled=True, quiet_minutes=30, max_files_auto=40)

def row(n=1, actions=(("review", ()),), head=A, seen=OLD, files=14, pipeline=False, title="t", ticket=""):
    return BoardRow(n, title, f"https://github.com/acme-org/acme-app/pull/{n}", "octo-author", ["reviewer"], head, seen,
                    False, files, [{"action": a, "threads": list(t)} for a, t in actions], [], False, pipeline, ticket,
                    "t", OLD)

def job(kind="review", head=A, state="queued", key=None, jid="j1"):
    return Job(jid, "acme/app", 1, kind, head, key or head, state, "", OLD, OLD)

def plan(rows, jobs=None, new=(), gone=(), running=True, first=False, skipped=lambda n, h: False, posting=frozenset(),
         held=frozenset(), cfg=CFG, answered=lambda j: frozenset(), pushed_from=lambda j: ""):
    return plan_jobs("acme/app", BoardUpdate({r.number: r for r in rows}, list(new), list(gone)), jobs or {},
                     skipped, cfg, running, NOW, first, posting=posting, held=held, answered=answered,
                     pushed_from=pushed_from)

def test_review_queued_after_quiet_window():
    assert plan([row()]).queue == [(1, "review", A, A)]
    assert plan([row(seen=RECENT)]).queue == []

def test_review_not_twice_and_skip_and_paused():
    assert plan([row()], {1: [job(state="done")]}).queue == []
    assert plan([row()], {1: [job(state="cancelled")]}).queue == [(1, "review", A, A)]
    assert plan([row()], skipped=lambda n, h: True).queue == []
    assert plan([row()], running=False).queue == []

def test_new_head_supersedes():
    p = plan([row(head=B)], {1: [job(head=A, state="queued", jid="q"), job(head=A, state="done", jid="d")]})
    assert p.queue == [(1, "review", B, B)]
    assert sorted(p.states) == [("d", "superseded", "new head"), ("q", "superseded", "new head")]

def test_large_pr_notice():
    p = plan([row(files=40)])
    assert p.queue == [] and p.notices == [Notice(1, "large", A,
        "[acme/app] #1 is large (40 files) — no automatic review · https://github.com/acme-org/acme-app/pull/1")]

def test_recheck_keyed_by_threads():
    r = row(actions=(("recheck", ("T2", "T1")),))
    assert plan([r]).queue == [(1, "recheck", A, f"{A}:T1,T2")]
    assert plan([r], {1: [job("recheck", key=f"{A}:T1,T2", state="done")]}).queue == []
    assert plan([row(actions=(("review", ()), ("recheck", ("T1",))))]).queue == [(1, "review", A, A)]

def test_gone_pr_cancels_queued():
    p = plan([], {1: [job(state="queued", jid="q"), job(state="done", jid="d")]},
             gone=[TurnChange(1, "review", ())])
    assert sorted(p.states) == [("d", "stale", "turn gone"), ("q", "cancelled", "turn gone")]

def test_turn_lines_and_first_poll_baseline():
    r = row(actions=(("merge", ()), ("answer_threads", ("T1", "T2", "T3"))))
    new = [TurnChange(1, "merge", ()), TurnChange(1, "answer_threads", ("T1", "T2", "T3"))]
    assert plan([r], new=new).lines == [
        "[acme/app] #1: your turn — merge · https://github.com/acme-org/acme-app/pull/1",
        "[acme/app] #1: replies in 3 thread(s) — handle them locally · https://github.com/acme-org/acme-app/pull/1"]
    assert plan([r], new=new, first=True).lines == []
    assert plan([row(actions=(("merge", ()),), pipeline=True)], new=new[:1]).lines == [
        "[acme/app] #1: your turn — merge · https://github.com/acme-org/acme-app/pull/1"]

def test_first_poll_baseline():
    p = plan([row(), row(2, actions=(("merge", ()),))], new=[TurnChange(1, "review", ()), TurnChange(2, "merge", ())],
             first=True)
    assert p.queue == [(1, "review", A, A)] and p.lines == []


def test_one_state_change_per_job_first_rule_wins():
    gone = [TurnChange(1, "review", ()), TurnChange(1, "review", ())]
    queued_old_head = {1: [job(head=A, state="queued", jid="q")]}
    assert plan([row(head=B)], queued_old_head, gone=gone).states == [("q", "superseded", "new head")]
    assert plan([], queued_old_head, gone=gone).states == [("q", "cancelled", "turn gone")]


def test_failure_and_doc_lines():
    assert readiness_failed_line("acme/app", "exit 3: boom", 10) == (
        "[acme/app] readiness command failed: exit 3: boom. Keeping the last board; next try in 10 min.")
    assert doc_missing_line("acme/app", "docs/review/code-review-process.md", "develop") == (
        "[acme/app] reviews are off: docs/review/code-review-process.md is not on develop.")


def test_poll_failed_line():
    assert poll_failed_line("acme/app", "GitHub 502", 10) == (
        "[acme/app] poll failed: GitHub 502. Keeping the last board; next try in 10 min.")


BOTH = (("review", ()), ("recheck", ("T1",)))
RECHECK_T1 = (1, "recheck", A, f"{A}:T1")


def test_recheck_runs_once_the_review_is_settled():
    assert plan([row(actions=BOTH)], skipped=lambda n, h: True).queue == [RECHECK_T1]
    large = plan([row(actions=BOTH, files=40)])
    assert large.queue == [RECHECK_T1] and [n.kind for n in large.notices] == ["large"]
    assert plan([row(actions=BOTH)], {1: [job(state="done")]}).queue == [RECHECK_T1]
    assert plan([row(actions=BOTH, seen=RECENT)]).queue == []
    assert plan([row(actions=BOTH)], {1: [job(state="queued")]}).queue == []
    assert plan([row(actions=BOTH)], {1: [job(state="running")]}).queue == []
    assert plan([row(actions=BOTH)], {1: [job(state="done", jid="d"), job(state="queued", jid="q")]}).queue == []
    # one failure is retried: the review is pending again, so the re-check still waits
    assert plan([row(actions=BOTH)], {1: [job(state="failed")]}).queue == [(1, "review", A, A)]


def test_large_notice_waits_for_quiet_window_not_for_switch():
    assert plan([row(files=40, seen=RECENT)]).notices == []
    assert plan([row(files=40)], running=False).notices == [Notice(1, "large", A,
        "[acme/app] #1 is large (40 files) — no automatic review · https://github.com/acme-org/acme-app/pull/1")]


def test_recheck_new_threads_supersede_queued():
    r = row(actions=(("recheck", ("T2", "T1")),))
    p = plan([r], {1: [job("recheck", key=f"{A}:T1", state="queued", jid="old")]})
    assert p.queue == [(1, "recheck", A, f"{A}:T1,T2")]
    assert p.states == [("old", "superseded", "new threads")]


def test_one_failure_retries_two_stop():
    assert plan([row()], {1: [job(state="failed")]}).queue == [(1, "review", A, A)]
    assert plan([row(seen=RECENT)], {1: [job(state="failed")]}).queue == []
    assert plan([row()], {1: [job(state="failed", jid="f1"), job(state="failed", jid="f2")]}).queue == []
    assert plan([row()], {1: [job(state="failed", head=B, jid="f1"), job(state="failed", jid="f2")]}).queue == [
        (1, "review", A, A)]


def test_failed_recheck_retried_until_two_failures():
    r = row(actions=(("recheck", ("T1",)),))
    key = f"{A}:T1"
    assert plan([r], {1: [job("recheck", key=key, state="failed")]}).queue == [(1, "recheck", A, key)]
    two = [job("recheck", key=key, state="failed", jid="f1"), job("recheck", key=key, state="failed", jid="f2")]
    assert plan([r], {1: two}).queue == []


def test_turn_back_restores_stale_report():
    p = plan([row()], {1: [job(state="stale", jid="j")]})
    assert p.states == [("j", "done", "turn back")] and p.queue == []
    assert plan([row()], {1: [job(state="done", jid="d"), job(state="stale", jid="j")]}).states == []
    older_stale = Job("s", "acme/app", 1, "review", A, A, "stale", "", OLD, OLD)
    newer_failed = Job("f", "acme/app", 1, "review", A, A, "failed", "", RECENT, RECENT)
    assert plan([row()], {1: [older_stale, newer_failed]}).states == []
    r = row(actions=(("recheck", ("T1",)),))
    p = plan([r], {1: [job("recheck", key=f"{A}:T1", state="stale", jid="r")]})
    assert p.states == [("r", "done", "turn back")] and p.queue == []


def test_pipeline_row_is_planned_like_any_other():
    p = plan([row(pipeline=True)], {1: [job(head=B, state="queued")]})
    assert p.queue == [(1, "review", A, A)] and p.states == [("j1", "superseded", "new head")]
    assert plan([row(head=B, seen=RECENT)], {1: [job(head=A, state="done")]}).states == []


def test_deferred_review_is_pending():
    assert plan([row()], {1: [job(state="deferred")]}).queue == []
    assert plan([row(actions=BOTH)], {1: [job(state="deferred")]}).queue == []


def test_new_head_supersedes_deferred():
    p = plan([row(head=B)], {1: [job(head=A, state="deferred", jid="d")]})
    assert ("d", "superseded", "new head") in p.states


def test_recheck_new_threads_supersede_deferred():
    r = row(actions=(("recheck", ("T2", "T1")),))
    p = plan([r], {1: [job("recheck", key=f"{A}:T1", state="deferred", jid="old")]})
    assert p.states == [("old", "superseded", "new threads")]


def test_gone_turn_cancels_deferred():
    p = plan([], {1: [job(state="deferred", jid="d")]}, gone=[TurnChange(1, "review", ())])
    assert p.states == [("d", "cancelled", "turn gone")]


def test_turn_back_ignores_stale_next_to_deferred():
    deferred = Job("d", "acme/app", 1, "review", A, A, "deferred", "", OLD, OLD)
    newer_stale = Job("s", "acme/app", 1, "review", A, A, "stale", "", RECENT, RECENT)
    p = plan([row()], {1: [deferred, newer_stale]})
    assert ("s", "done", "turn back") not in p.states


def test_gone_turn_never_cancels_a_review_now():
    review_now = [job(state="queued", key=f"{A}:now", jid="q"), job(state="deferred", key=f"{A}:now", jid="d")]
    assert plan([row(actions=())], {1: review_now}, gone=[TurnChange(1, "review", ())]).states == []
    assert plan([], {1: review_now}, gone=[TurnChange(1, "review", ())]).states == []


# --- Plan 5: posted reports ---

def test_new_head_supersedes_posted_review():
    p = plan([row(head=B)], {1: [job(head=A, state="posted")]})
    assert p.queue == [(1, "review", B, B)]
    assert p.states == [("j1", "superseded", "new head")]


def test_new_recheck_supersedes_older_done_and_posted():
    r = row(actions=(("recheck", ("T3",)),))
    older = [job("recheck", key=f"{A}:T1", state="done", jid="d"),
             job("recheck", key=f"{A}:T1,T2", state="posted", jid="p")]
    p = plan([r], {1: older})
    assert p.queue == [(1, "recheck", A, f"{A}:T3")]
    assert sorted(p.states) == [("d", "superseded", "new threads"), ("p", "superseded", "new threads")]


def test_posted_recheck_blocks_until_my_thread_turns_change():
    """A posted re-check blocks its key until the operator's own thread turns change after the post: another
    reviewer's turn, a role or the draft flag moving the row's last_change does not unblock it (/code-review C6)."""
    r = dataclasses.replace(row(actions=(("recheck", ("T1",)),)), thread_turns_change=OLD)
    key = f"{A}:T1"
    posted = {1: [job("recheck", key=key, state="posted")]}  # its updated_at is OLD, the post's time
    assert plan([r], posted).queue == [] and plan([r], posted).states == []
    later = "2026-11-20T11:01:00+00:00"  # a minute after the post
    approved = dataclasses.replace(r, last_change=later, other_turns=[{"login": "octo-2", "action": "review"}])
    assert plan([approved], posted).queue == [] and plan([approved], posted).states == []
    mine = dataclasses.replace(r, last_change=later, thread_turns_change=later)
    p = plan([mine], posted)
    assert p.queue == [(1, "recheck", A, key)]
    assert p.states == [("j1", "superseded", "new threads")]


def test_a_post_under_way_keeps_its_job_from_going_stale():
    """The gone-turn rule leaves a job with a Post `posting` alone: the post itself takes the turn away
    (/code-review C3). Other jobs, and other rules, are as before."""
    gone = [TurnChange(1, "review", ()), TurnChange(1, "recheck", ())]
    jobs = {1: [job(state="done", jid="d"), job("recheck", state="done", jid="r"), job(state="queued", jid="q")]}
    assert sorted(plan([], jobs, gone=gone, posting=frozenset({"d", "r"})).states) == [("q", "cancelled", "turn gone")]
    assert sorted(plan([], jobs, gone=gone).states) == [
        ("d", "stale", "turn gone"), ("q", "cancelled", "turn gone"), ("r", "stale", "turn gone")]
    assert plan([row(head=B)], {1: [job(state="queued", jid="d")]}, posting=frozenset({"d"})).states == [
        ("d", "superseded", "new head")]


def test_a_held_pr_gets_no_review_and_no_recheck():
    rows = [row(1), row(2, actions=(("recheck", ("T1",)),)), row(3)]
    assert plan(rows, held=frozenset({1, 2})).queue == [(3, "review", A, A)]


def test_held_prs_are_the_compared_tickets_marked_prs():
    rows = {r.number: r for r in [
        row(1, title="ACME-1: Fix login [staged]", ticket="ACME-1"),
        row(2, title="ACME-1: Fix login [skill]", ticket="ACME-1"),
        row(3, title="ACME-1: Fix login", ticket="ACME-1"),          # the winner, retitled, or a person's PR
        row(4, title="ACME-2: Other [skill]", ticket="ACME-2"),      # a ticket not compared now
    ]}
    assert held_prs(rows, frozenset({"ACME-1"})) == frozenset({1, 2})
    assert held_prs(rows, frozenset()) == frozenset()


# --- Address jobs ---

ADDR = dataclasses.replace(CFG, process=ProcessPartConfig(address_doc="docs/threads.md"))

def author_row(actions=(("answer_threads", ("T2", "T1")),), **kw):
    return dataclasses.replace(row(actions=actions, **kw), roles=["author"])

def test_address_waits_while_a_ticket_workspace_holds_the_pr():
    held = author_row(pipeline=True)
    p = plan([held], cfg=ADDR, new=[TurnChange(1, "answer_threads", ("T1", "T2"))])
    assert p.queue == [] and len(p.lines) == 1                       # no job; the turn line is sent
    done = job("address", key=f"{A}:T1,T2", state="done")
    assert plan([held], {1: [done]}, cfg=ADDR).states == []          # a job that ran before stays as it is
    assert plan([author_row()], cfg=ADDR).queue == [(1, "address", A, f"{A}:T1,T2")]   # the flag dropped

def test_address_skips_a_held_pr_and_sends_its_turn_line():      # one side of a comparison with the skill flow
    p = plan([author_row()], cfg=ADDR, held=frozenset({1}), new=[TurnChange(1, "answer_threads", ("T1", "T2"))])
    assert p.queue == [] and len(p.lines) == 1

def test_address_queued_for_the_author_after_the_quiet_window():
    assert plan([author_row()], cfg=ADDR).queue == [(1, "address", A, f"{A}:T1,T2")]
    assert plan([author_row()]).queue == []                                           # no address_doc
    assert plan([row(actions=(("answer_threads", ("T1",)),))], cfg=ADDR).queue == []  # not the author
    assert plan([dataclasses.replace(author_row(), thread_turns_change=RECENT)], cfg=ADDR).queue == []
    assert plan([author_row()], cfg=ADDR, running=False).queue == []

def test_address_key_joins_the_threads_of_all_three_actions():
    r = author_row(actions=(("answer_threads", ("T2",)), ("resolve_threads", ("T1",)), ("address_review", ())))
    assert plan([r], cfg=ADDR).queue == [(1, "address", A, f"{A}:T1,T2")]
    assert plan([author_row(actions=(("address_review", ()),))], cfg=ADDR).queue == [(1, "address", A, f"{A}:")]

def test_address_not_queued_while_a_job_covers_the_turn():
    done = job("address", key=f"{A}:T1,T2,T3", state="done")
    for state in ("queued", "running", "deferred", "done", "posted"):
        assert plan([author_row()], {1: [dataclasses.replace(done, state=state)]}, cfg=ADDR).queue == []
    back = plan([author_row()], {1: [dataclasses.replace(done, state="posted")]}, cfg=ADDR,
                answered=lambda j: frozenset({"T2"}))      # T2's reply landed and T2 is my turn again
    assert back.queue == [(1, "address", A, f"{A}:T1,T2")] and back.states == [("j1", "superseded", "new threads")]
    new_thread = author_row(actions=(("answer_threads", ("T9",)),))
    assert plan([new_thread], {1: [done]}, cfg=ADDR).queue == [(1, "address", A, f"{A}:T9")]
    assert plan([author_row(head=B)], {1: [done]}, cfg=ADDR).queue == [(1, "address", B, f"{B}:T1,T2")]

def test_address_not_queued_while_the_board_is_behind_a_push():
    """After "push" the job is at the new head B. A poll that still shows the old head A (GitHub's list lags) must
    not queue a job at A and supersede the pushed one."""
    pushed = job("address", head=B, key=f"{B}:T1,T2", state="done")
    behind = plan([author_row()], {1: [pushed]}, cfg=ADDR, pushed_from=lambda j: A)
    assert behind.queue == [] and behind.states == []
    caught_up = plan([author_row(head=B)], {1: [pushed]}, cfg=ADDR, pushed_from=lambda j: A)
    assert caught_up.queue == [] and caught_up.states == []
    other = plan([author_row()], {1: [pushed]}, cfg=ADDR)      # a job at B that was not pushed from A
    assert other.queue == [(1, "address", A, f"{A}:T1,T2")]

def test_address_not_queued_while_one_is_pending_at_the_head():
    """Spec, "No new job while one covers the turn": a pending job at this head blocks a new one, whatever its
    threads; a new thread is taken up once it ended."""
    for state in ("queued", "running", "deferred"):
        pending = job("address", key=f"{A}:T1", state=state)
        assert plan([author_row()], {1: [pending]}, cfg=ADDR).queue == []
    queued_old_head = job("address", key=f"{A}:T1", state="queued")
    p = plan([author_row(head=B)], {1: [queued_old_head]}, cfg=ADDR)
    assert p.queue == [(1, "address", B, f"{B}:T1,T2")] and p.states == [("j1", "superseded", "new threads")]

def test_address_two_failures_stop():
    f = job("address", key=f"{A}:T1,T2", state="failed")
    assert plan([author_row()], {1: [f]}, cfg=ADDR).queue == [(1, "address", A, f"{A}:T1,T2")]
    assert plan([author_row()], {1: [f, dataclasses.replace(f, id="j2")]}, cfg=ADDR).queue == []

def test_address_turn_back_revives_instead_of_queueing():
    stale = job("address", key=f"{A}:T1,T2", state="stale")
    p = plan([author_row()], {1: [stale]}, cfg=ADDR)
    assert p.queue == [] and p.states == [("j1", "done", "turn back")]

def test_gone_address_turn_cancels_and_stales():
    q = job("address", key=f"{A}:T1", state="queued", jid="q")
    d = job("address", key=f"{A}:T1", state="done", jid="d")
    gone = [TurnChange(1, "answer_threads", ("T1",))]
    p = plan([author_row(actions=(("merge", ()),))], {1: [q, d]}, cfg=ADDR, gone=gone)
    assert sorted(p.states) == [("d", "stale", "turn gone"), ("q", "cancelled", "turn gone")]
    still = author_row(actions=(("resolve_threads", ("T1",)),))      # another address action is left
    assert plan([still], {1: [q, d]}, cfg=ADDR, gone=gone).states == []

def test_address_actions_send_no_turn_line_where_address_is_on():
    r = author_row(actions=(("merge", ()), ("answer_threads", ("T1",))))
    new = [TurnChange(1, "merge", ()), TurnChange(1, "answer_threads", ("T1",))]
    assert plan([r], new=new, cfg=ADDR).lines == [
        "[acme/app] #1: your turn — merge · https://github.com/acme-org/acme-app/pull/1"]
    assert len(plan([r], new=new).lines) == 2
    assert len(plan([dataclasses.replace(r, pipeline=True)], new=new, cfg=ADDR).lines) == 2
    not_mine = dataclasses.replace(r, roles=["reviewer"])     # no address job for it: the line is its notice
    assert len(plan([not_mine], new=new, cfg=ADDR).lines) == 2
