# tests/unit/test_reviewer_posting_replies.py
from __future__ import annotations

import asyncio
import copy
import json
import stat
from pathlib import Path

import pytest

from config.schemas import REPLIES_PLACEHOLDERS, ReviewerConfig
from orchestrator.reviewer import posting as posting_module
from orchestrator.reviewer.clone import ReviewClone
from orchestrator.reviewer.commands import CommandResult
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.grammar import ReplyCommand, parse_reply
from orchestrator.reviewer.models import JobRun, Post, PRFull, RepoState
from orchestrator.reviewer.posting import Posting, PostOutcome, validate_plan
from orchestrator.reviewer.readiness import ReadinessError
from orchestrator.reviewer.service import RepoContext
from orchestrator.reviewer.store import ReviewerStore
from tests.unit.test_reviewer_poster_output import RESULTS, STEPS
from tests.unit.test_reviewer_posting import (
    ALLOWED,
    HEAD_MOVED,
    INTERNAL_AFTER,
    NEW_HEAD,
    PR_URL,
    SCOPE,
    T0,
    T1,
    BusyClone,
    N,
    Script,
    not_shown,
)
from tests.unit.test_reviewer_posting import Rig as ReviewRig
from tests.unit.test_reviewer_report import HEAD

ME, AUTHOR = "octo", "octo-author"
CMD = ReplyCommand("post_replies", (1, 3))
REPLY_1, RESOLVE_1, COMMENT_3 = json.loads(STEPS)
REPLIED, _, COMMENTED = (r.get("replied") or r.get("commented") for r in json.loads(RESULTS))
RESOLVED = " · resolved (reply not seen on GitHub: resolved threads are not listed)"


def comment(comment_id, author, body="…"):
    return {"id": comment_id, "author": author, "bot": False, "body": body, "created_at": "2026-11-20T09:00:00Z"}


def thread(thread_id, turn, *comments):
    return {"id": thread_id, "outdated": False, "path": "app/src/main/CartRepository.kt", "line": 88,
            "comments": list(comments), "turn": turn, "awaits_answer": True}


def review_body(review_id, answered):
    return {"id": review_id, "author": AUTHOR, "state": "COMMENTED", "body": "…",
            "submitted_at": "2026-11-20T09:00:00Z", "answered": answered}


def snapshot(threads, bodies):
    """A threads snapshot as the threads command prints it: unresolved threads only."""
    return {"number": N, "author": AUTHOR, "head_sha": HEAD, "threads": threads, "review_bodies": bodies}


THREAD_1 = thread("T1", ME, comment(101, ME), comment(102, AUTHOR))
THREAD_2 = thread("T2", AUTHOR, comment(201, ME))
BODIES = [review_body(456, False), review_body(457, True)]
PREPARED = snapshot([THREAD_1, THREAD_2], BODIES)  # Prepare's, and GitHub's right before the confirm run
AFTER = snapshot([THREAD_2], [review_body(456, True), review_body(457, True)])  # T1 resolved, 456 answered
PLAN = [
    {"n": 1, "thread_id": "T1", "comment_id": 101, "body": "Fixed.", "resolve": True},
    {"n": 2, "thread_id": "T2", "body": "Still on it.", "resolve": False},
    {"n": 3, "review_id": 456, "body": "Agreed."},
    {"n": 4, "review_id": 457, "body": "Thanks."},
    {"n": 5, "thread_id": "T1", "comment_id": 102, "body": "See above.", "resolve": False},
    {"n": 6, "thread_id": "T1", "comment_id": 101, "body": "Done.", "resolve": True, "fix_sha": "abc1234"},
    {"n": 7, "thread_id": "T9", "body": "Gone?", "resolve": False},
]


def steps(*items):
    return json.dumps(list(items))


class Poster:
    """The replies poster: records (values, confirm) and the plan file each run reads, and "dry" / "confirm" in
    the rig's log; returns the scripted results (a str is the stdout of an exit 0)."""

    def __init__(self, log, dry, confirm, dry_wait):
        self.log, self.calls, self.plans, self.dry_wait = log, [], [], dry_wait
        self.dry, self.confirm = Script(*dry), Script(*confirm)
        self.on_dry = self.on_confirm = lambda: None

    async def replies(self, clone_dir, values, *, confirm):
        self.calls.append((values, confirm))
        self.plans.append(json.loads(Path(values["plan"]).read_text(encoding="utf-8")))
        self.log.append("confirm" if confirm else "dry")
        if not confirm:
            await asyncio.sleep(self.dry_wait)
        (self.on_confirm if confirm else self.on_dry)()
        result = (self.confirm if confirm else self.dry).next()
        return result if isinstance(result, CommandResult) else CommandResult(0, result, "", False)


class Readiness:
    """The threads command as scripted, one snapshot per call; records where it ran and whether the clone's lock
    was held."""

    repo = "acme-org/acme-app"

    def __init__(self, log, clone, snapshots):
        self.log, self.clone, self.calls = log, clone, []
        self.snapshots = Script(*snapshots)
        self.on_threads = lambda count: None  # called with the threads call's count, from 1

    async def threads(self, clone_dir, pr):
        self.log.append("threads")
        self.calls.append((clone_dir, pr, self.clone.lock.locked()))
        self.on_threads(self.log.count("threads"))
        return copy.deepcopy(self.snapshots.next())


class Forge:
    def __init__(self, log, heads, state):
        self.log, self.heads, self.state = log, Script(*heads), state

    async def pr_full(self, number):
        self.log.append("pr_full")
        return PRFull(number, "Fix cart sync", "", PR_URL, self.state, self.heads.next(), "develop", 20, 10, 14)


class Rig:
    """A done re-check job of PR 1712 at HEAD in a real store, its copy holding Prepare's threads snapshot and its
    run the replies plan; a fake poster, threads command and forge sharing one log, a real ReviewClone over an
    empty dir, a sleep that records."""

    def __init__(self, tmp_path, *, plan=PLAN, dry=(STEPS,), confirm=(RESULTS,), heads=(HEAD,),
                 snapshots=(PREPARED, AFTER), prepared=PREPARED, pr_state="open", dry_wait=0.0, clone=None,
                 store=None, posting=None, log=None):
        self.log, self.waits = [] if log is None else log, []
        self.store = store or ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))
        self.store.save_repo_state(RepoState(SCOPE, first_poll_done=True, me=ME))
        self.job = self.store.add_job(SCOPE, N, "recheck", HEAD, f"recheck:{HEAD}", T0.isoformat())
        self.store.set_job_state(self.job.id, "done", "", T0.isoformat())
        self.job_dir = tmp_path / "rv" / "jobs" / self.job.id
        self.job_dir.mkdir(parents=True)
        if prepared is not None:
            (self.job_dir / "threads.prepared.json").write_text(json.dumps(prepared), encoding="utf-8")
        self.store.save_run(JobRun(self.job.id, started_at=T0.isoformat(), job_dir=str(self.job_dir), url=PR_URL,
                                   replies=plan))
        self.clone = clone or ReviewClone(tmp_path / "clone", "file:///nowhere", "develop", "ghp_not_real")
        self.poster = Poster(self.log, dry, confirm, dry_wait)
        self.readiness = Readiness(self.log, self.clone, snapshots)
        self.forge = Forge(self.log, heads, pr_state)
        self.ctx = RepoContext("acme", "app", ReviewerConfig(), self.clone, self.readiness, self.forge,
                               poster=self.poster, allowed_links=ALLOWED)
        self.posting = posting or Posting(self.store, ledgers_dir=tmp_path / "rv" / "ledgers", now=lambda: T1,
                                          sleep=self._sleep)

    async def _sleep(self, seconds):
        self.waits.append(seconds)

    async def post(self, cmd=CMD):
        return await self.posting.post_replies(self.ctx, self.job, cmd)

    def state(self):
        return self.store.job(self.job.id).state

    def posts(self):
        return self.store.posts(SCOPE, N)

    def confirms(self):
        return [values for values, confirm in self.poster.calls if confirm]


def test_validate_plan():
    valid, invalid = validate_plan(PLAN, PREPARED, ME)
    assert valid == {1: PLAN[0], 3: PLAN[2]}
    assert invalid == {
        2: "thread T2 is not your turn",
        4: "review 457 is answered already",
        5: "comment 102 is not the first comment of thread T1",
        6: "carries fix_sha",
        7: "thread T9 is not an unresolved thread of this PR",
    }
    # Neither or both of thread_id / review_id; an id of another JSON type is not this PR's; a login in any case.
    odd = [{"n": 8, "body": "x"}, {"n": 9, "thread_id": "T1", "review_id": 456, "body": "x"},
           {"n": 10, "review_id": 456.0, "body": "x"}, {"n": 11, "thread_id": "T1", "comment_id": 101.0, "body": "x"}]
    assert validate_plan(odd, PREPARED, ME) == ({}, {
        8: "names neither a thread nor a review",
        9: "names neither a thread nor a review",
        10: "review 456.0 is not a review body of this PR",
        11: "comment 101.0 is not the first comment of thread T1",
    })
    assert validate_plan(PLAN, PREPARED, "OCTO")[0].keys() == {1, 3}


async def test_post_replies_happy(tmp_path):
    rig = Rig(tmp_path, plan=[{**PLAN[0], "gist": "a model's note"}, *PLAN[1:]])
    out = await rig.post()
    assert out == PostOutcome("posted", f"Posted replies on #1712:\n1: replied {REPLIED}{RESOLVED}\n"
                                        f"3: answered {COMMENTED}")
    values = rig.poster.calls[0][0]
    assert rig.poster.calls == [(values, False), (values, True)]  # the confirm argv differs only in the flag
    assert set(values) == REPLIES_PLACEHOLDERS
    plan = rig.job_dir / "posting" / "replies_26c4929c5.json"
    assert (values["repo"], values["pr"], values["head9"], values["plan"], values["job_dir"]) == (
        "acme-org/acme-app", "1712", "26c4929c5", str(plan), str(rig.job_dir))
    assert rig.poster.plans == [[PLAN[0], PLAN[2]]] * 2  # the named entries, their known keys only
    assert stat.S_IMODE((rig.job_dir / "posting").stat().st_mode) == 0o700
    assert rig.log == ["pr_full", "dry", "pr_full", "threads", "confirm", "threads"]
    job = rig.store.job(rig.job.id)
    assert (job.state, job.updated_at) == ("posted", T1.isoformat())  # the post's time
    [post] = rig.posts()
    assert (post.kind, post.state, post.numbers, post.event, post.urls, post.comments, post.updated_at) == (
        "replies", "posted", [1, 3], "", [REPLIED, COMMENTED], 2, T1.isoformat())
    assert [(item["n"], item["where"]) for item in post.landed] == [(1, "reply"), (1, "resolve"), (3, "comment")]
    assert rig.store.posted_replies(rig.job) == {1, 3}
    assert rig.store.posted_count(SCOPE, N, HEAD) == (0, False)  # replies are not review comments


async def test_validates_against_prepared_threads(tmp_path):
    session = snapshot([THREAD_1, {**THREAD_2, "turn": ME}], BODIES)  # the run directory's, as the session left it
    two = ReplyCommand("post_replies", (2,))
    rig = Rig(tmp_path)
    (rig.job_dir / "threads.json").write_text(json.dumps(session), encoding="utf-8")
    assert await rig.post(two) == PostOutcome(
        "refused", "#2 cannot be posted: thread T2 is not your turn. Nothing was done.")
    assert rig.poster.calls == []

    rig = Rig(tmp_path / "absent", prepared=None)
    (rig.job_dir / "threads.json").write_text(json.dumps(session), encoding="utf-8")
    assert await rig.post(two) == PostOutcome(
        "refused", "Prepare's threads snapshot is not in this job's copy: nothing posted.")
    assert rig.poster.calls == []

    rig = Rig(tmp_path / "unreadable")
    (rig.job_dir / "threads.prepared.json").write_text("{not json", encoding="utf-8")
    assert await rig.post(two) == PostOutcome(
        "refused", "Not posted on #1712: Prepare's threads snapshot in this job's copy is unreadable.")
    assert rig.poster.calls == []


async def test_threads_read_only_under_the_lease(tmp_path):
    rig = Rig(tmp_path, snapshots=(PREPARED, PREPARED, AFTER))  # GitHub lists the change on the second read
    assert (await rig.post()).state == "posted"
    # Before the confirm run, then both post-condition reads: from the clone, under its lock.
    assert rig.readiness.calls == [(rig.clone.path, N, True)] * 3
    assert rig.waits == [2.0]
    assert not rig.clone.lock.locked()


async def test_unknown_replies_output_line(tmp_path):
    rig = Rig(tmp_path, dry=("Plan OK\n" + STEPS,))
    assert await rig.post() == PostOutcome(
        "refused", "Not posted on #1712: the dry run's output was not understood: the replies dry run is not one"
                   " JSON list.")
    assert rig.confirms() == [] and rig.posts() == []

    # The confirm run's output not understood: the post-condition still runs, and records what GitHub shows. The
    # reply in the thread now resolved is the tool's report alone, which is unknown (/code-review C2).
    rig = Rig(tmp_path / "confirm", confirm=(RESULTS + "\ndone",))
    out = await rig.post()
    why = ("#1: the reply is not seen (a resolved thread's replies are not listed); the tool's output was not"
           " understood (the replies result is not one JSON list)")
    assert out == PostOutcome("unverified", f"Partly posted replies on #1712 — {why}.\n1: resolved\n3: answered"
                                            f" {PR_URL}\n#1: thread T1 was resolved, but the reply in it is not seen;"
                                            f" reply on GitHub yourself if it is missing.{CHECK}")
    [post] = rig.posts()
    assert (post.state, post.error) == ("unverified", why)
    assert [(item["n"], item["where"]) for item in post.landed] == [(1, "resolve"), (3, "comment")]
    assert rig.state() == "done" and rig.store.posted_numbers(rig.job, None) == {3}
    assert rig.log[rig.log.index("confirm"):] == ["confirm", "threads", "threads", "threads"]


async def test_a_confirm_run_that_timed_out_is_checked_on_github(tmp_path):
    """The replies tool posts 2 of 3 replies, then times out (its output is lost): the threads command shows the 2,
    which are recorded as landed and named in the answer (/code-review C2)."""
    plan = [{"n": 1, "thread_id": "T1", "comment_id": 101, "body": "Fixed.", "resolve": False}, PLAN[2],
            {"n": 8, "review_id": 458, "body": "Will do."}]
    prepared = snapshot([THREAD_1, THREAD_2], [*BODIES, review_body(458, False)])
    after = snapshot([{**THREAD_1, "comments": [*THREAD_1["comments"], comment(103, ME, "Fixed.")]}, THREAD_2],
                     [review_body(456, True), review_body(457, True), review_body(458, False)])
    dry = steps(REPLY_1, COMMENT_3, {"step": "comment", "n": 8, "target": "review 458 @octo-author", "body": "Will do."})
    rig = Rig(tmp_path, plan=plan, prepared=prepared, dry=(dry,), confirm=(CommandResult(-9, "", "", True),),
              snapshots=(prepared, after))
    assert await rig.post(ReplyCommand("post_replies", (1, 3, 8))) == PostOutcome(
        "unverified", "Partly posted replies on #1712 — #8: review 458 is not answered; the tool timed out.\n"
                      f'1: replied {PR_URL}\n3: answered {PR_URL}{CHECK}\nReply "post replies 8" to post the rest.')
    [post] = rig.posts()
    assert post.state == "unverified"
    assert [(item["n"], item["where"]) for item in post.landed] == [(1, "reply"), (3, "comment")]
    assert rig.store.posted_numbers(rig.job, None) == {1, 3} and rig.state() == "done"
    # Neither is posted again.
    assert await rig.post(ReplyCommand("post_replies", (1, 8))) == PostOutcome(
        "refused", "#1 is already posted at this head. Nothing was done.")


async def test_an_internal_error_once_the_replies_post_ran_is_unverified(tmp_path, monkeypatch):
    """As for a review (/code-review C4): never "failed" once the confirm run ran."""
    def broken(*args):
        raise KeyError("n")

    rig = Rig(tmp_path)
    monkeypatch.setattr(posting_module, "_landed_text", broken)  # after the Post is saved posted
    assert await rig.post() == PostOutcome("unverified", INTERNAL_AFTER.format("KeyError"))
    assert rig.posts()[0].state == "posted" and rig.state() == "posted"

    rig = Rig(tmp_path / "in-the-post-condition")
    monkeypatch.setattr(posting_module, "_seen", broken)
    assert await rig.post() == PostOutcome("unverified", INTERNAL_AFTER.format("KeyError"))
    [post] = rig.posts()
    assert (post.state, post.landed, post.error) == ("unverified", [], "internal error (KeyError)")


async def test_replies_posted_twice_refused(tmp_path):
    rig = Rig(tmp_path)
    rig.store.save_post(Post("p0", rig.job.id, SCOPE, N, HEAD, "replies", "posted", T1.isoformat(), numbers=[1],
                             landed=[{"n": 1, "path": "", "line": None, "title": "", "where": "reply"}]))
    assert await rig.post() == PostOutcome("refused", "#1 is already posted at this head. Nothing was done.")
    assert rig.log == [] and rig.poster.calls == []

    # A landing GitHub showed counts whatever the Post's final state (fix round 1).
    for state in ("failed", "unverified"):
        rig = Rig(tmp_path / state)
        rig.store.save_post(Post("p0", rig.job.id, SCOPE, N, HEAD, "replies", state, T1.isoformat(), numbers=[1, 3],
                                 landed=[{"n": 3, "path": "", "line": None, "title": "", "where": "comment"}]))
        assert await rig.post() == PostOutcome("refused", "#3 is already posted at this head. Nothing was done.")
        assert rig.log == []

    # Both arriving before the first lands: the second is refused under the posting lock.
    rig = Rig(tmp_path / "gathered", dry_wait=0.05)
    one, two = await asyncio.gather(rig.post(), rig.post())
    assert one.state == "posted"
    assert two == PostOutcome("refused", "#1 is already posted at this head. Nothing was done.")
    assert [confirm for _, confirm in rig.poster.calls] == [False, True]


async def test_replies_and_reviews_share_one_lock(tmp_path):
    review = ReviewRig(tmp_path / "review", dry_wait=0.05)
    rig = Rig(tmp_path / "replies", store=review.store, posting=review.posting, log=review.log, dry_wait=0.05)
    first, second = await asyncio.gather(review.post(), rig.post())
    assert (first.state, second.state) == ("posted", "posted")
    # The re-check's replies start only after the review's post-condition.
    assert review.log == ["pr_full", "reviews", "dry", "pr_full", "reviews", "confirm", "reviews", "review_comments",
                          "pr_full", "dry", "pr_full", "threads", "confirm", "threads"]


async def test_resolve_needs_replied_result(tmp_path):
    rig = Rig(tmp_path, confirm=(json.dumps([{"n": 1, "resolved": True}, {"n": 3, "commented": COMMENTED}]),))
    out = await rig.post()
    # Fix round 1: what landed is partly posted, never "Not posted"; the missing reply is the operator's to check.
    why = "#1: the tool reports no reply (a resolved thread's replies are not listed)"
    assert out == PostOutcome("partial", f"Partly posted replies on #1712 — {why}.\n1: resolved\n3: answered"
                                         f" {COMMENTED}\n#1: thread T1 was resolved, but the tool reports no reply in"
                                         " it; reply on GitHub yourself if it is missing.")
    assert rig.log.count("threads") == 4 and rig.waits == [2.0, 2.0]  # before, then 3 reads
    [post] = rig.posts()
    assert (post.state, post.error, post.urls, post.comments) == ("partial", why, [COMMENTED], 1)
    assert [(item["n"], item["where"]) for item in post.landed] == [(1, "resolve"), (3, "comment")]
    assert rig.state() == "posted" and rig.store.posted_replies(rig.job) == {3}


async def test_reply_landed_resolve_failed(tmp_path):
    t1 = {**THREAD_1, "comments": [*THREAD_1["comments"], comment(103, ME, "Fixed.")]}
    after = snapshot([t1, THREAD_2], [review_body(456, True), review_body(457, True)])
    results = json.dumps([{"n": 1, "replied": REPLIED}, {"n": 3, "commented": COMMENTED}])
    rig = Rig(tmp_path, confirm=(CommandResult(1, results, "resolve: HTTP 403\n", False),), snapshots=(PREPARED, after))
    assert await rig.post() == PostOutcome(
        "partial", "Partly posted replies on #1712 — #1: thread T1 is still unresolved; the tool exited 1: resolve:"
                   f" HTTP 403.\n1: replied {REPLIED}\n3: answered {COMMENTED}\n#1: your reply is in thread T1; resolve"
                   " it on GitHub yourself.")
    [post] = rig.posts()
    assert [(item["n"], item["where"]) for item in post.landed] == [(1, "reply"), (3, "comment")]
    assert rig.store.posted_replies(rig.job) == {1, 3} and rig.state() == "posted"
    # Neither is posted again, whatever the tool's own ledger would say.
    for numbers in ((1, 3), (3,)):
        assert await rig.post(ReplyCommand("post_replies", numbers)) == PostOutcome(
            "refused", f"#{numbers[0]} is already posted at this head. Nothing was done.")
    assert [confirm for _, confirm in rig.poster.calls] == [False, True]


async def test_body_problem_on_recheck_reply(tmp_path):
    plan = [{**entry, "body": "@octo2 look"} if entry["n"] == 3 else entry for entry in PLAN]
    rig = Rig(tmp_path, plan=plan)
    assert await rig.post() == PostOutcome(
        "refused", "#3: its text mentions @octo2: nothing posted. A re-check reply cannot be edited here:"
                   " answer that thread on GitHub yourself.")
    assert rig.poster.calls == [] and not (rig.job_dir / "posting").exists()


async def test_named_invalid_entry_refused(tmp_path):
    rig = Rig(tmp_path)
    assert await rig.post(ReplyCommand("post_replies", (1, 2))) == PostOutcome(
        "refused", "#2 cannot be posted: thread T2 is not your turn. Nothing was done.")
    assert rig.poster.calls == [] and rig.log == ["pr_full"]


NO_RESOLVE = [{**PLAN[0], "resolve": False}, *PLAN[1:]]
STEP_MISMATCHES = {
    "unnamed": (steps(REPLY_1, RESOLVE_1, COMMENT_3, {**REPLY_1, "n": 2}), "the dry run plans reply for #2"),
    "no-resolve": (steps(REPLY_1, COMMENT_3), "the dry run plans reply for #1"),
    "reply-twice": (steps(REPLY_1, REPLY_1, RESOLVE_1, COMMENT_3), "the dry run plans reply + reply + resolve for #1"),
    "review-reply": (steps(REPLY_1, RESOLVE_1, {**COMMENT_3, "step": "reply"}), "the dry run plans reply for #3"),
    "resolve-not-planned": (STEPS, "the dry run plans reply + resolve for #1", NO_RESOLVE),
    # Its reply is not in the thread: the tool would resolve without it.
    "resolve-only": (steps(RESOLVE_1, COMMENT_3), "the dry run plans resolve for #1"),
    "swapped-texts": (steps({**REPLY_1, "body": "Agreed."}, RESOLVE_1, {**COMMENT_3, "body": "Fixed."}),
                      "the reply for #1 is not its planned text"),
    "other-text-and-target": (steps({**REPLY_1, "body": "LGTM, merging.", "target": "app/Other.kt:1 @someone"},
                                    RESOLVE_1, COMMENT_3), "the reply for #1 is not its planned text"),
    "other-target": (steps({**REPLY_1, "target": "app/Other.kt:1 @someone"}, RESOLVE_1, COMMENT_3),
                     "the reply for #1 is aimed at app/Other.kt:1 @someone"),
    "other-review": (steps(REPLY_1, RESOLVE_1, {**COMMENT_3, "target": "review 457 @octo-author"}),
                     "the comment for #3 is aimed at review 457 @octo-author"),
    "resolve-with-text": (steps(REPLY_1, {**RESOLVE_1, "body": "Closing."}, COMMENT_3),
                          "the resolve for #1 carries a text"),
    "body-link": (steps({**REPLY_1, "body": "Fixed: https://evil.example"}, RESOLVE_1, COMMENT_3),
                  "the reply for #1 is not its planned text"),
    "exit-1": (CommandResult(1, STEPS, "T1: not an unresolved thread\n", False),
               "the dry run failed: T1: not an unresolved thread"),
    "timed-out": (CommandResult(-9, "", "", True, 300.0), "the dry run failed: timed out after 300 s"),  # C10
}


@pytest.mark.parametrize("case", list(STEP_MISMATCHES.values()), ids=list(STEP_MISMATCHES))
async def test_dry_run_extra_step_stops(tmp_path, case):
    dry, why, plan = (*case, PLAN)[:3]
    rig = Rig(tmp_path, dry=(dry,), plan=plan)
    assert await rig.post() == PostOutcome("refused", f"Not posted on #1712: {why}.")
    assert rig.confirms() == [] and rig.posts() == [] and rig.state() == "done"


async def test_step_text_still_passes_the_body_check(tmp_path):
    plan = [{**entry, "body": "Fixed.\n```\nhttps://evil.example\n```"} if entry["n"] == 1 else entry for entry in PLAN]
    # The same words, but the fence no longer opens a line: the link is no longer code.
    rig = Rig(tmp_path, plan=plan, dry=(steps({**REPLY_1, "body": "Fixed. ```\nhttps://evil.example\n```"}, RESOLVE_1,
                                              COMMENT_3),))
    assert await rig.post() == PostOutcome("refused", "Not posted on #1712: the reply for #1 links to"
                                                      " https://evil.example.")
    assert rig.confirms() == []


@pytest.mark.parametrize("target, posted", [
    ("T1", True), ("T1 @octo-author", True), ("app/src/main/CartRepository.kt:88 @octo-author", True),
    # Fix round 2: the path alone, or the path at another line, is not T1 (Q3).
    ("app/src/main/CartRepository.kt @octo-author", False), ("app/src/main/CartRepository.kt:5 @x", False),
    ("app/src/main/CartRepository.kt:088 @x", False), ("T2 @octo-author", False),
])
async def test_step_targets_name_the_thread_or_its_line(tmp_path, target, posted):
    rig = Rig(tmp_path, dry=(steps({**REPLY_1, "target": target}, {**RESOLVE_1, "target": target}, COMMENT_3),))
    out = await rig.post()
    if posted:
        assert out.state == "posted"
    else:
        assert out == PostOutcome("refused", f"Not posted on #1712: the reply for #1 is aimed at {target}.")
        assert rig.confirms() == []


ANSWERED = [review_body(456, True), review_body(457, True)]


async def test_landed_entry_reported(tmp_path):
    # GitHub agrees that #3 is posted: review 456 is answered before the confirm run.
    rig = Rig(tmp_path, dry=(steps(REPLY_1, RESOLVE_1),),
              confirm=(json.dumps([{"n": 1, "replied": REPLIED}, {"n": 1, "resolved": True}]),),
              snapshots=(snapshot([THREAD_1, THREAD_2], ANSWERED), snapshot([THREAD_2], ANSWERED)))
    assert await rig.post() == PostOutcome(
        "posted", f"Posted replies on #1712:\n1: replied {REPLIED}{RESOLVED}\n3: already posted")
    [post] = rig.posts()
    assert [(item["n"], item["where"]) for item in post.landed] == [(1, "reply"), (1, "resolve")]

    # T1 is gone, or no longer my turn, or holds my reply; 456 is answered.
    t1_states = ([], [{**THREAD_1, "turn": AUTHOR}], [{**THREAD_1, "comments": [comment(103, ME, "Fixed.")]}])
    for k, t1 in enumerate(t1_states):
        rig = Rig(tmp_path / f"nothing-{k}", dry=("[]",), snapshots=(snapshot([*t1, THREAD_2], ANSWERED),))
        assert await rig.post() == PostOutcome(
            "nothing", "Nothing left to post on #1712 at 26c4929c5: what you named is already posted.")
        assert rig.confirms() == [] and rig.posts() == [] and rig.state() == "done"


async def test_left_out_entry_needs_github_to_agree(tmp_path):
    # P6: the dry run leaves #3 out as posted, but GitHub shows review 456 unanswered before the confirm run.
    rig = Rig(tmp_path, dry=(steps(REPLY_1, RESOLVE_1),),
              confirm=(json.dumps([{"n": 1, "replied": REPLIED}, {"n": 1, "resolved": True}]),),
              snapshots=(PREPARED, snapshot([THREAD_2], BODIES)))
    assert await rig.post() == PostOutcome(
        "unverified", "Partly posted replies on #1712 — #3: left out as already posted, but review 456 is not"
                      f" answered.\n1: replied {REPLIED}{RESOLVED}{CHECK}")
    [post] = rig.posts()
    assert post.state == "unverified" and rig.store.posted_replies(rig.job) == {1}
    assert rig.state() == "done"

    # Nothing planned at all: nothing is posted, and the operator checks the PR.
    rig = Rig(tmp_path / "nothing", dry=("[]",))
    assert await rig.post() == PostOutcome(
        "unverified", "Nothing posted on #1712: the replies tool has nothing to post, but GitHub does not show"
                      " everything posted (#1: left out as already posted, but thread T1 is still your turn;"
                      " #3: left out as already posted, but review 456 is not answered). Check the PR on GitHub:"
                      f" {PR_URL}")
    assert rig.confirms() == [] and rig.posts() == [] and rig.state() == "done"


T1_OPEN = snapshot([THREAD_1, THREAD_2], [review_body(456, True), review_body(457, True)])  # 456 answered only
NOT_SEEN = "#1: no new reply of yours in thread T1, thread T1 is still unresolved"
CHECK = f"\nCheck the PR on GitHub before posting again: {PR_URL}"


@pytest.mark.parametrize("confirm, out", [
    # Fix round 1: #3 landed, so the post is partly posted, with the command for the rest.
    (CommandResult(1, json.dumps([{"n": 3, "commented": COMMENTED}]), "T1: HTTP 422\n", False),
     PostOutcome("partial", f"Partly posted replies on #1712 — {NOT_SEEN}; the tool exited 1: T1: HTTP 422.\n"
                            f'3: answered {COMMENTED}\nReply "post replies 1" to post the rest.')),
    # The tool reports #1 replied and resolved, which the threads command does not show: it may have landed.
    (RESULTS, PostOutcome("unverified", f"Partly posted replies on #1712 — {NOT_SEEN}.\n3: answered {COMMENTED}"
                                        f'{CHECK}\nReply "post replies 1" to post the rest.')),
], ids=["not-reported", "reported"])
async def test_reply_not_found_after_confirm(tmp_path, confirm, out):
    rig = Rig(tmp_path, confirm=(confirm,), snapshots=(PREPARED, T1_OPEN))
    assert await rig.post() == out
    assert rig.log.count("threads") == 4
    [post] = rig.posts()
    assert post.state == out.state and [(item["n"], item["where"]) for item in post.landed] == [(3, "comment")]
    assert rig.state() == ("posted" if out.state == "partial" else "done")
    assert rig.store.posted_replies(rig.job) == {3}
    assert parse_reply("post replies 1") == ReplyCommand("post_replies", (1,))

    # Nothing landed and the tool reports nothing: "Not posted".
    rig = Rig(tmp_path / "none", confirm=(CommandResult(1, "[]", "HTTP 502\n", False),), snapshots=(PREPARED,))
    assert await rig.post() == PostOutcome(
        "failed", "Not posted on #1712: #1: no new reply of yours in thread T1, thread T1 is still unresolved;"
                  " #3: review 456 is not answered; the tool exited 1: HTTP 502.")
    assert rig.posts()[0].landed == [] and rig.state() == "done"


def with_comments(*comments):
    return snapshot([{**THREAD_1, "comments": [*THREAD_1["comments"], *comments]}, THREAD_2], BODIES)


REPLIED_1 = json.dumps([{"n": 1, "replied": REPLIED}])
NO_REPLY = PostOutcome("unverified", not_shown("#1: no new reply of yours in thread T1"))  # the tool reports it


@pytest.mark.parametrize("after, confirm, out", [
    (with_comments(comment(103, "Octo", "Fixed.")), REPLIED_1,
     PostOutcome("posted", f"Posted replies on #1712:\n1: replied {REPLIED}")),
    (with_comments(comment(103, AUTHOR, "Fixed.")), REPLIED_1, NO_REPLY),
    (PREPARED, REPLIED_1, NO_REPLY),  # 101 is mine but not new
    (with_comments(comment(103, ME, "laptop")), REPLIED_1, NO_REPLY),  # a comment of mine with other text
    # P1c: the tool fails and reports nothing; a comment of mine with other text is not the reply.
    (with_comments(comment(103, ME, "laptop")), CommandResult(1, "[]", "boom\n", False),
     PostOutcome("failed", "Not posted on #1712: #1: no new reply of yours in thread T1; the tool exited 1: boom.")),
], ids=["new-reply-of-mine", "someone-elses", "only-old-ones", "other-text", "other-text-tool-failed"])
async def test_reply_seen_in_its_thread(tmp_path, after, confirm, out):
    plan = [{"n": 1, "thread_id": "T1", "comment_id": 101, "body": "Fixed.", "resolve": False}]
    rig = Rig(tmp_path, plan=plan, dry=(steps(REPLY_1),), confirm=(confirm,), snapshots=(PREPARED, after))
    assert await rig.post(ReplyCommand("post_replies", (1,))) == out
    assert rig.posts()[0].state == out.state


async def test_results_report_an_unplanned_entry(tmp_path):
    results = json.dumps([*json.loads(RESULTS), {"n": 2, "replied": f"{PR_URL}#discussion_r9"}])
    rig = Rig(tmp_path, confirm=(results,))
    assert await rig.post() == PostOutcome(
        "unverified", "Partly posted replies on #1712 — the tool reports a reply for #2 that was not planned.\n"
                      f"1: replied {REPLIED}{RESOLVED}\n3: answered {COMMENTED}{CHECK}")
    assert rig.posts()[0].state == "unverified" and rig.state() == "done"


REPLY_IN_T1 = with_comments(comment(103, ME, "Fixed."))


@pytest.mark.parametrize("before, dry, text", [
    (snapshot([{**THREAD_1, "turn": AUTHOR}, THREAD_2], BODIES), STEPS,
     "#1 cannot be posted: thread T1 is not your turn. Nothing was done."),
    (snapshot([THREAD_2], BODIES), STEPS,
     "#1 cannot be posted: thread T1 is not an unresolved thread of this PR. Nothing was done."),
    (snapshot([THREAD_1, THREAD_2], ANSWERED), STEPS,
     "#3 cannot be posted: review 456 is answered already. Nothing was done."),
    # #1's reply landed earlier, its resolve did not: whether the tool's ledger knows the reply or not.
    (REPLY_IN_T1, STEPS, "#1: your reply is in thread T1; resolve it on GitHub yourself. Nothing was done."),
    (REPLY_IN_T1, steps(RESOLVE_1, COMMENT_3),
     "#1: your reply is in thread T1; resolve it on GitHub yourself. Nothing was done."),
], ids=["turn-moved", "thread-gone", "review-answered", "reply-in-thread", "reply-in-thread-resolve-only"])
async def test_github_right_before_the_confirm_must_allow_it(tmp_path, before, dry, text):
    rig = Rig(tmp_path, dry=(dry,), snapshots=(before,))
    assert await rig.post() == PostOutcome("refused", text)
    assert rig.confirms() == [] and rig.posts() == [] and rig.log.count("threads") == 1


async def test_reply_already_in_its_thread_is_not_posted_again(tmp_path):
    plan = [{"n": 1, "thread_id": "T1", "comment_id": 101, "body": "Fixed.", "resolve": False}]
    rig = Rig(tmp_path, plan=plan, dry=(steps(REPLY_1),), snapshots=(with_comments(comment(103, ME, "Fixed.")),))
    assert await rig.post(ReplyCommand("post_replies", (1,))) == PostOutcome(
        "refused", "#1: your reply is in thread T1 already. Nothing was done.")
    assert rig.confirms() == []


# A second round in T1: I said "Fixed." before Prepare, the author answered, and it is my turn again.
T1_ROUND_2 = thread("T1", ME, comment(100, AUTHOR, "Please fix"), comment(101, ME, "Fixed."),
                    comment(102, AUTHOR, "Still broken"))
PREPARED_2 = snapshot([T1_ROUND_2, THREAD_2], BODIES)
PLAN_2 = [{"n": 1, "thread_id": "T1", "comment_id": 100, "body": "Fixed.", "resolve": True}, PLAN[2]]
NEW_FIXED = snapshot([{**T1_ROUND_2, "comments": [*T1_ROUND_2["comments"], comment(103, ME, "Fixed.")]}, THREAD_2],
                     BODIES)  # the same words again, posted after Prepare


async def test_an_earlier_identical_reply_is_not_this_one(tmp_path):
    # Q1: my "Fixed." of an earlier round is in Prepare's snapshot: the new reply is posted.
    rig = Rig(tmp_path, plan=PLAN_2, prepared=PREPARED_2, snapshots=(PREPARED_2, AFTER))
    assert await rig.post() == PostOutcome("posted", f"Posted replies on #1712:\n1: replied {REPLIED}{RESOLVED}\n"
                                                     f"3: answered {COMMENTED}")
    # One posted after Prepare still is: it is not posted again.
    rig = Rig(tmp_path / "new", plan=PLAN_2, prepared=PREPARED_2, snapshots=(NEW_FIXED,))
    assert await rig.post() == PostOutcome(
        "refused", "#1: your reply is in thread T1; resolve it on GitHub yourself. Nothing was done.")
    assert rig.confirms() == []


async def test_an_earlier_identical_reply_does_not_agree_it_is_posted(tmp_path):
    # Q2: the dry run leaves #1 out, but only my earlier "Fixed." is in T1, and it is still my turn.
    dry, confirm = steps(COMMENT_3), json.dumps([{"n": 3, "commented": COMMENTED}])
    rig = Rig(tmp_path, plan=PLAN_2, prepared=PREPARED_2, dry=(dry,), confirm=(confirm,),
              snapshots=(PREPARED_2, snapshot([T1_ROUND_2, THREAD_2], ANSWERED)))
    assert await rig.post() == PostOutcome(
        "unverified", "Partly posted replies on #1712 — #1: left out as already posted, but thread T1 is still your"
                      f" turn.\n3: answered {COMMENTED}{CHECK}")
    assert rig.state() == "done" and rig.store.posted_replies(rig.job) == {3}
    # One posted after Prepare does agree.
    rig = Rig(tmp_path / "new", plan=PLAN_2, prepared=PREPARED_2, dry=(dry,), confirm=(confirm,),
              snapshots=(NEW_FIXED, snapshot(NEW_FIXED["threads"], ANSWERED)))
    assert await rig.post() == PostOutcome(
        "posted", f"Posted replies on #1712:\n1: already posted\n3: answered {COMMENTED}")


@pytest.mark.parametrize("where, snapshots, out", [
    ("prepared", (PREPARED,), PostOutcome(
        "refused", "Not posted on #1712: Prepare's threads snapshot is of #1712 at 8f01a2b3c.")),
    ("before-head", ({**PREPARED, "head_sha": NEW_HEAD},), PostOutcome("refused", HEAD_MOVED)),
    ("before-number", ({**PREPARED, "number": 1713},), PostOutcome(
        "refused", "Not posted on #1712: the threads command shows #1713 at 26c4929c5.")),
    ("after", (PREPARED, {**AFTER, "head_sha": NEW_HEAD}), PostOutcome(
        "unverified", "Posting on #1712 ran, but GitHub could not be read to check it (the threads command shows"
                      f" #1712 at 8f01a2b3c). Check the PR before posting again: {PR_URL}")),
], ids=["prepared", "before-head", "before-number", "after"])
async def test_snapshot_of_another_pr_or_head_refused(tmp_path, where, snapshots, out):
    rig = Rig(tmp_path, snapshots=snapshots, prepared={**PREPARED, "head_sha": NEW_HEAD} if where == "prepared"
              else PREPARED)
    assert await rig.post() == out
    assert [confirm for _, confirm in rig.poster.calls] == ([False, True] if where == "after" else
                                                            [] if where == "prepared" else [False])


@pytest.mark.parametrize("heads, pr_state, text, runs", [
    ((NEW_HEAD,), "open", HEAD_MOVED, []),
    ((HEAD, NEW_HEAD), "open", HEAD_MOVED, [False]),  # between the dry run and the confirm run
    ((HEAD,), "closed", "PR #1712 is closed: nothing posted.", []),
    ((ForgeError("GitHub 502", unavailable=True),), "open",
     "GitHub could not be read for #1712 (GitHub 502): nothing posted.", []),
], ids=["before-dry-run", "before-confirm", "closed", "unreadable"])
async def test_head_moved_refuses_replies(tmp_path, heads, pr_state, text, runs):
    rig = Rig(tmp_path, heads=heads, pr_state=pr_state)
    assert await rig.post() == PostOutcome("refused", text)
    assert [confirm for _, confirm in rig.poster.calls] == runs
    assert rig.posts() == [] and rig.state() == "done"


async def test_refused_before_github_is_read(tmp_path):
    rig = Rig(tmp_path)
    rig.store.set_job_state(rig.job.id, "superseded", "newer job", T0.isoformat())  # rig.job still reads done
    assert await rig.post() == PostOutcome("refused", "Outdated: there is a newer review. Nothing was done.")
    rig = Rig(tmp_path / "unpolled")
    rig.store.save_repo_state(RepoState(SCOPE))
    assert await rig.post() == PostOutcome("refused", "Waiting for the first poll: nothing posted.")
    rig = Rig(tmp_path / "not-replies")
    for cmd in (ReplyCommand("post", (1,)), ReplyCommand("review_again")):
        assert await rig.post(cmd) == PostOutcome("refused", "Not a post command. Nothing was done.")
    assert rig.log == []


async def test_job_outdated_right_before_the_post_refuses(tmp_path):
    rig = Rig(tmp_path)
    rig.readiness.on_threads = lambda count: rig.store.set_job_state(
        rig.job.id, "superseded", "newer job", T1.isoformat())  # during the read right before the post
    assert await rig.post() == PostOutcome("refused", "Outdated: there is a newer review. Nothing was done.")
    assert rig.confirms() == [] and rig.posts() == []


async def test_a_job_superseded_during_the_post_condition_stays_superseded(tmp_path):
    """As for a review (final review M1): a verified post leaves a job superseded meanwhile as it is."""
    rig = Rig(tmp_path)
    rig.readiness.on_threads = lambda count: count == 2 and rig.store.set_job_state(
        rig.job.id, "superseded", "new head", T1.isoformat())  # the post-condition's read
    assert (await rig.post()).state == "posted"
    assert rig.posts()[0].state == "posted" and rig.state() == "superseded"


async def test_clone_in_use_refused(tmp_path):
    rig = Rig(tmp_path, clone=BusyClone(tmp_path / "clone", "file:///nowhere", "develop", "ghp_not_real"))
    assert await rig.post() == PostOutcome(
        "refused", "Not posted on #1712: the review clone is in use; try again in a minute.")
    assert rig.poster.calls == [] and rig.posts() == []


async def test_threads_command_failure(tmp_path):
    rig = Rig(tmp_path, snapshots=(ReadinessError("exit 1: HTTP 502"),))
    assert await rig.post() == PostOutcome(
        "refused", "GitHub could not be read for #1712 (the threads command failed: exit 1: HTTP 502): nothing posted.")
    assert rig.confirms() == [] and rig.posts() == []

    rig = Rig(tmp_path / "after", snapshots=(PREPARED, ReadinessError("exit 1: HTTP 502")))
    assert await rig.post() == PostOutcome(
        "unverified", "Posting on #1712 ran, but GitHub could not be read to check it (exit 1: HTTP 502). Check the"
                      f" PR before posting again: {PR_URL}")
    [post] = rig.posts()
    assert (post.state, post.error) == ("unverified", "exit 1: HTTP 502") and rig.state() == "done"


async def test_posting_planted_as_a_file_refused(tmp_path):
    rig = Rig(tmp_path)
    (rig.job_dir / "posting").write_text("not Cleave's")
    assert await rig.post() == PostOutcome(
        "refused", "Not posted on #1712: the job copy's posting directory is not Cleave's.")
    assert rig.poster.calls == []


async def test_cancelled_post_is_left_to_recover(tmp_path):
    rig = Rig(tmp_path, confirm=(asyncio.CancelledError(),))
    with pytest.raises(asyncio.CancelledError):
        await rig.post()
    [post] = rig.posts()
    assert (post.kind, post.state, post.error) == ("replies", "posting", "") and rig.state() == "done"
    assert not rig.clone.lock.locked()  # the lease was let go
    [recovered] = rig.posting.recover()
    assert (recovered.id, recovered.state) == (post.id, "interrupted")
