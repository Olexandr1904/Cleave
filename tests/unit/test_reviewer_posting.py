# tests/unit/test_reviewer_posting.py
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import stat
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config.schemas import POSTER_PLACEHOLDERS, ReviewerConfig
from orchestrator.reviewer import posting as posting_module
from orchestrator.reviewer.clone import CloneError, ReviewClone
from orchestrator.reviewer.commands import CommandResult
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.grammar import ReplyCommand, parse_reply
from orchestrator.reviewer.lines import interrupted_line
from orchestrator.reviewer.models import ForgeReview, JobRun, Post, PRFull, RepoState
from orchestrator.reviewer.poster_output import parse_review_dry_run
from orchestrator.reviewer.posting import (
    EVENT_STATE,
    DryRunPlan,
    Posting,
    PostOutcome,
    check_dry_run,
)
from orchestrator.reviewer.report import Report, parse_report
from orchestrator.reviewer.report_edit import apply_draft, apply_edit, ensure_base, write_atomic
from orchestrator.reviewer.service import RepoContext
from orchestrator.reviewer.store import ReviewerStore
from tests.unit.test_reviewer_poster_output import DRY
from tests.unit.test_reviewer_report import HEAD, REPORT

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)  # the run's start
T1 = T0 + timedelta(hours=2)  # Cleave's clock while it posts
SCOPE = "acme/app"
N = 1712
PR_URL = "https://github.com/acme-org/acme-app/pull/1712"
ALLOWED = ("https://github.com/acme-org/",)
NEW_HEAD = "8f01a2b3c" + "b" * 31
OTHER_SHA = "deadbeef0" + "c" * 31
NAME = f"report_{HEAD[:9]}.md"
CMD = ReplyCommand("post", (1, 2, 5), "APPROVE")
APPROVE_ONLY = ReplyCommand("post", (), "APPROVE", no_summary=True)
BODY = json.loads(DRY.split("--- DRY RUN batch 1/1 ---\n")[1])["body"]  # the payload's review body
SUMMARY = "Two real problems in the sync path."
C1 = {"path": "app/src/main/CartRepository.kt", "line": 88, "side": "RIGHT",
      "body": "**NPE on an empty list**\n\nThe list is read before the null check."}
C2 = {"path": "app/src/main/SyncWorker.kt", "line": 43, "side": "RIGHT",
      "body": "**Retry without backoff**\n\nNetwork retry rule.\n\n_Anchor moved from line 41._"}
ITEM_LINES = {1: "  #01 app/src/main/CartRepository.kt:88  - NPE on an empty list",
              2: "  #02 app/src/main/SyncWorker.kt:41  - Retry without backoff",
              5: "  #05 app/src/main/res/values/strings.xml:212  - Untranslated key?"}
UNAVAILABLE = ForgeError("GitHub 502", unavailable=True)


def review(review_id, state="APPROVED", login="octo", commit=HEAD, at=T1 + timedelta(minutes=1), body=BODY):
    """A review as GitHub lists it, submitted_at in GitHub's Z form."""
    return ForgeReview(review_id, login, state, commit, at.strftime("%Y-%m-%dT%H:%M:%SZ"), body,
                       f"{PR_URL}#pullrequestreview-{review_id}")


NEW = review(11)  # what the confirm run of DRY posts
APPROVED = review(12, body="")  # what an approve without a body posts
COMMENTS = {11: [{"path": C1["path"], "line": 88, "body": C1["body"]},
                 {"path": C2["path"], "line": 43, "body": C2["body"]}]}
CONFIRMED = f"Posted batch 1/1: {NEW.url}\n  app/src/main/CartRepository.kt:88\n  app/src/main/SyncWorker.kt:43\n"


def dry_run(numbers, *comment_lists, body=BODY, event="APPROVE", summary="yes"):
    """A dry run listing `numbers`, one `event` payload at HEAD per comment list, the first with `body`."""
    payloads = [{"commit_id": HEAD, "event": event, "comments": comments} for comments in comment_lists]
    if payloads:
        payloads[0]["body"] = body
    count = sum(map(len, comment_lists))
    lines = [f"Event  : {event}", f"Summary: {summary}", f"Inline : {count} comment(s)",
             *(ITEM_LINES[n] for n in numbers), f"HEAD   : {HEAD}",
             f"Posting {count} comment(s) in {len(payloads)} review(s)"]
    for i, payload in enumerate(payloads, 1):
        lines += [f"--- DRY RUN batch {i}/{len(payloads)} ---", json.dumps(payload)]
    return "\n".join(lines) + "\n"


class Script:
    """Results handed out in order, the last one repeating; an exception is raised instead of returned."""

    def __init__(self, *results):
        self._results = list(results)

    def next(self):
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(result, BaseException):
            raise result
        return result


class FakePoster:
    """Records (values, confirm) per run and "dry" / "confirm" in the rig's log; returns the scripted results (a
    str is the stdout of an exit 0). A dry run sleeps `dry_wait` seconds first."""

    def __init__(self, log, dry, confirm, dry_wait):
        self.log, self.calls, self.dry_wait = log, [], dry_wait
        self.dry, self.confirm = Script(*dry), Script(*confirm)
        self.on_dry = lambda: None

    async def review(self, clone_dir, values, *, confirm):
        self.calls.append((values, confirm))
        self.log.append("confirm" if confirm else "dry")
        if not confirm:
            await asyncio.sleep(self.dry_wait)
            self.on_dry()
        result = (self.confirm if confirm else self.dry).next()
        return result if isinstance(result, CommandResult) else CommandResult(0, result, "", False)


class FakeForge:
    """GitHub as scripted: the PR's head per pr_full call, the review list per reviews call, each review's
    comments, the approve outcome; every call goes to the rig's log."""

    def __init__(self, log, heads, reviews, comments, approve, state):
        self.log, self.comments, self.state, self.approvals = log, comments, state, []
        self.heads, self.approve_outcome = Script(*heads), Script(*approve)
        self.script_reviews(*reviews)
        self.on_reviews = lambda count: None  # called with the reviews call's count, from 1

    def script_reviews(self, *lists):
        self.review_lists = Script(*lists)

    async def pr_full(self, number):
        self.log.append("pr_full")
        head = self.heads.next()
        return PRFull(number, "Fix cart sync", "", PR_URL, self.state, head, "develop", 20, 10, 14)

    async def reviews(self, number):
        self.log.append("reviews")
        self.on_reviews(self.log.count("reviews"))
        return list(self.review_lists.next())

    async def review_comments(self, number, review_id):
        self.log.append("review_comments")
        comments = self.comments.get(review_id, [])
        if isinstance(comments, BaseException):
            raise comments
        return comments

    async def approve(self, number, head):
        self.log.append("approve")
        self.approvals.append((number, head))
        return self.approve_outcome.next()


class Readiness:
    repo = "acme-org/acme-app"


class Rig:
    """A done review job of PR 1712 at HEAD in a real store (its run started at T0, its copy holding the report),
    a fake poster and forge sharing one log, a real ReviewClone over an empty dir, a sleep that records."""

    def __init__(self, tmp_path, *, dry=(DRY,), confirm=(CONFIRMED,), heads=(HEAD,), reviews=([], [], [NEW]),
                 comments=COMMENTS, approve=(APPROVED,), pr_state="open", dry_wait=0.0, clone=None):
        self.log, self.waits = [], []
        self.store = ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))
        self.store.save_repo_state(RepoState(SCOPE, first_poll_done=True, me="octo"))
        self.job = self.store.add_job(SCOPE, N, "review", HEAD, HEAD, T0.isoformat())
        self.store.set_job_state(self.job.id, "done", "", T0.isoformat())
        self.job_dir = tmp_path / "rv" / "jobs" / self.job.id
        self.job_dir.mkdir(parents=True)
        (self.job_dir / NAME).write_text(REPORT, encoding="utf-8")
        self.store.save_run(JobRun(self.job.id, started_at=T0.isoformat(), job_dir=str(self.job_dir), url=PR_URL,
                                   report=parse_report(REPORT, HEAD).to_dict()))
        self.poster = FakePoster(self.log, dry, confirm, dry_wait)
        self.forge = FakeForge(self.log, heads, reviews, comments, approve, pr_state)
        self.clone = clone or ReviewClone(tmp_path / "clone", "file:///nowhere", "develop", "ghp_not_real")
        self.ctx = RepoContext("acme", "app", ReviewerConfig(), self.clone, Readiness(), self.forge,
                               poster=self.poster, allowed_links=ALLOWED)
        self.ledgers = tmp_path / "rv" / "ledgers"
        self.posting = Posting(self.store, ledgers_dir=self.ledgers, now=lambda: T1, sleep=self._sleep)

    async def _sleep(self, seconds):
        self.waits.append(seconds)

    async def post(self, cmd=CMD):
        return await self.posting.post_review(self.ctx, self.job, cmd)

    def state(self):
        return self.store.job(self.job.id).state

    def posts(self):
        return self.store.posts(SCOPE, N)

    def confirms(self):
        return [values for values, confirm in self.poster.calls if confirm]


async def test_post_happy_path(tmp_path):
    rig = Rig(tmp_path)
    out = await rig.post()
    assert out == PostOutcome("posted", f"Posted on #1712 at 26c4929c5 — APPROVE, 1 review: {NEW.url}\n"
                                        "inline: 1 2 · moved anchor: 2 · in the review body: 5")
    values = rig.poster.calls[0][0]
    assert rig.poster.calls == [(values, False), (values, True)]
    assert set(values) == POSTER_PLACEHOLDERS
    assert values["ledger"].endswith("ledgers/acme/app/PR_1712/posted_26c4929c5.json")
    assert values["ledger"] == str(rig.posting.ledger_path(rig.ctx, N, HEAD))
    assert (values["repo"], values["pr"], values["head9"], values["event"]) == (
        "acme-org/acme-app", "1712", "26c4929c5", "APPROVE")
    assert (values["report"], values["job_dir"]) == (str(rig.job_dir / NAME), str(rig.job_dir))
    assert values["plan"] == str(rig.job_dir / "posting" / "replies_26c4929c5.json")
    ticked = (rig.job_dir / NAME).read_text(encoding="utf-8")
    assert re.findall(r"^### \[x\] (\d+)\.", ticked, re.MULTILINE) == ["1", "2", "5"]
    base = (rig.job_dir / "posting" / "report.base.md").read_text(encoding="utf-8")
    assert "### [x]" not in base and base.count("### [ ] ") == 6
    for directory in (rig.ledgers, rig.ledgers / "acme", rig.ledgers / "acme" / "app",
                      rig.ledgers / "acme" / "app" / "PR_1712"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    job = rig.store.job(rig.job.id)
    assert (job.state, job.updated_at) == ("posted", T1.isoformat())  # the post's time
    [post] = rig.posts()
    assert (post.kind, post.state, post.numbers, post.event, post.updated_at) == (
        "review", "posted", [1, 2, 5], "APPROVE", T1.isoformat())
    assert (post.summary_posted, post.comments, post.review_ids, post.urls) == (True, 2, [11], [NEW.url])
    assert [(item["n"], item["where"]) for item in post.landed] == [(1, "inline"), (2, "moved"), (5, "body")]
    moved = post.landed[1]
    assert (moved["path"], moved["line"], moved["title"]) == ("app/src/main/SyncWorker.kt", 41, "Retry without backoff")
    assert ("app/src/main/SyncWorker.kt", 41, "Retry without backoff") in rig.store.posted_keys(SCOPE, N, HEAD)


@pytest.mark.parametrize("state, reason, text", [
    ("superseded", "new head", "Outdated: there is a review of a newer head. Nothing was done."),
    ("superseded", "newer job", "Outdated: there is a newer review. Nothing was done."),
    ("stale", "turn gone", "Outdated: this report no longer matches the PR (turn gone). Nothing was done."),
    ("failed", "boom", 'This job ended failed: only "review again" and "skip" work here. Nothing was done.'),
])
async def test_refuses_outdated_jobs(tmp_path, state, reason, text):
    rig = Rig(tmp_path)
    rig.store.set_job_state(rig.job.id, state, reason, T0.isoformat())  # rig.job still reads done
    assert await rig.post() == PostOutcome("refused", text)
    assert rig.log == [] and rig.posts() == []


async def test_waits_for_the_first_poll(tmp_path):
    rig = Rig(tmp_path)
    rig.store.save_repo_state(RepoState(SCOPE))
    assert await rig.post() == PostOutcome("refused", "Waiting for the first poll: nothing posted.")
    assert rig.log == []


async def test_refuses_closed_pr(tmp_path):
    rig = Rig(tmp_path, pr_state="closed")
    assert await rig.post() == PostOutcome("refused", "PR #1712 is closed: nothing posted.")
    assert rig.poster.calls == [] and rig.posts() == []


HEAD_MOVED = 'Head moved (26c4929c5 → 8f01a2b3c): nothing posted. Reply "review again" for a review of the new head.'


async def test_head_moved_before_dry_run(tmp_path):
    rig = Rig(tmp_path, heads=(NEW_HEAD,))
    assert await rig.post() == PostOutcome("refused", HEAD_MOVED)
    assert rig.poster.calls == [] and rig.posts() == []


async def test_head_moved_between_dry_run_and_confirm(tmp_path):
    rig = Rig(tmp_path, heads=(HEAD, NEW_HEAD))
    assert await rig.post() == PostOutcome("refused", HEAD_MOVED)
    assert [confirm for _, confirm in rig.poster.calls] == [False]
    assert rig.posts() == [] and rig.state() == "done"


ALREADY = ('You already submitted a review on this PR after the report (2026-11-20T10:30:00Z): nothing posted.'
           ' Add "anyway" to your reply to post regardless.')


async def test_already_reviewed_after_report(tmp_path):
    laptop = review(7, state="COMMENTED", at=T0 + timedelta(minutes=30))
    pending = ForgeReview(8, "octo", "PENDING", HEAD, "", "", f"{PR_URL}#pullrequestreview-8")
    rig = Rig(tmp_path, reviews=([pending, laptop],))
    assert await rig.post() == PostOutcome("refused", ALREADY)
    assert rig.poster.calls == [] and rig.posts() == []
    rig.forge.script_reviews([laptop], [laptop], [laptop, NEW])
    assert (await rig.post(dataclasses.replace(CMD, anyway=True))).state == "posted"

    # A review whose id a Post of Cleave's recorded is not the operator's own.
    rig = Rig(tmp_path / "recorded", reviews=([laptop], [laptop], [laptop, NEW]))
    rig.store.save_post(Post("p0", "j0", SCOPE, N, HEAD, "review", "failed", T0.isoformat(), review_ids=[7]))
    assert (await rig.post()).state == "posted"

    # A review submitted before the run started, or by someone else, is not after the report.
    early = review(7, state="COMMENTED", at=T0 - timedelta(minutes=1))
    other = review(9, state="COMMENTED", login="octo-author", at=T0 + timedelta(minutes=30))
    rig = Rig(tmp_path / "early", reviews=([early, other], [early, other], [early, other, NEW]))
    assert (await rig.post()).state == "posted"


async def test_interrupted_post_needs_anyway(tmp_path):
    # What an interrupted COMMENT post landed: its id never recorded.
    landed = review(9, state="COMMENTED", at=T1 - timedelta(minutes=5))
    rig = Rig(tmp_path, reviews=([landed],))
    rig.store.save_post(Post("p0", rig.job.id, SCOPE, N, HEAD, "review", "interrupted", T1.isoformat(),
                             numbers=[1, 2, 5], event="COMMENT"))
    out = await rig.post()
    assert out.state == "refused"
    assert out.text.startswith("You already submitted a review on this PR after the report (")
    assert rig.poster.calls == []
    rig.forge.script_reviews([landed], [landed], [landed, NEW])
    assert (await rig.post(dataclasses.replace(CMD, anyway=True))).state == "posted"
    # Had it landed an approval, an APPROVE is refused even with "anyway" (fix round 1).
    rig = Rig(tmp_path / "approved", reviews=([review(9, at=T1 - timedelta(minutes=5))],))
    assert await rig.post(dataclasses.replace(CMD, anyway=True)) == PostOutcome(
        "refused", "Already approved #1712 at 26c4929c5. Nothing was done.")


async def test_body_with_mention_refused(tmp_path):
    rig = Rig(tmp_path)
    run = rig.store.run(rig.job.id)
    report = Report.from_dict(run.report)
    edited = dataclasses.replace(report.items[0], body="@octo2 look")
    run.report = dataclasses.replace(report, items=(edited, *report.items[1:])).to_dict()
    rig.store.save_run(run)
    assert await rig.post() == PostOutcome(
        "refused", '#1: its text mentions @octo2: nothing posted. Fix it with "edit 1: …".')
    assert rig.poster.calls == []


POSTING_LINE = "Posting 2 comment(s) in 1 review(s)\n"
ANCHORS_LINE = "Anchors: 2 inline, 1 degraded to body\n"
BODY_SECTION = ("\\n\\n### Findings without a diff anchor\\n\\n**Untranslated key?** "
                "`app/src/main/res/values/strings.xml:212`")
MISMATCHES = {
    "item-number": (DRY.replace("#05", "#04"), "the dry run lists #1 2 4 instead of #1 2 5"),
    "head-line": (DRY.replace(f"HEAD   : {HEAD}", f"HEAD   : {OTHER_SHA}"),
                  "the dry run's HEAD is deadbeef0, not 26c4929c5"),
    "header-event": (DRY.replace("Event  : APPROVE", "Event  : COMMENT"),
                     "the dry run's event is COMMENT, not APPROVE"),
    "review-count": (DRY.replace(POSTING_LINE, "Posting 2 comment(s) in 2 review(s)\n"),
                     "the dry run plans 2 review(s) but shows 1"),
    "comment-count": (DRY.replace(POSTING_LINE, "Posting 3 comment(s) in 1 review(s)\n"),
                      "the dry run plans 3 comment(s) but shows 2"),
    "commit-id": (DRY.replace(f'"commit_id": "{HEAD}"', f'"commit_id": "{OTHER_SHA}"'),
                  "a payload is for commit deadbeef0"),
    "payload-event": (DRY.replace('"event": "APPROVE"', '"event": "COMMENT"'), "a payload's event is COMMENT"),
    "empty-commit-id": (DRY.replace(f'"commit_id": "{HEAD}"', '"commit_id": ""'), "a payload is for commit "),
    "empty-event": (DRY.replace('"event": "APPROVE"', '"event": ""'), "a payload's event is "),
    "foreign-comment": (DRY.replace(f'"path": "{C1["path"]}"', '"path": "app/x.kt"')
                        .replace("**NPE on an empty list**", "**Other**"),
                        "the comment on app/x.kt:88 matches no named number"),
    "matched-twice": (dry_run((1, 2, 5), [C1, {**C1, "line": 43}]),
                      "the comment on app/src/main/CartRepository.kt:43 matches #1 twice"),
    "title-without-its-text": (DRY.replace("The list is read before the null check.", "Something else."),
                               "the comment on app/src/main/CartRepository.kt:88 matches no named number"),
    "unnamed-item-in-the-body": (dry_run((1, 2, 5), [C1, C2], body=BODY + "\n\n**Cursor leak** `LegacySync.kt:300`"),
                                 "the review body holds #6, which you did not name"),
    "unnamed-item-in-a-comment": (dry_run((1, 2, 5), [{**C1, "body": C1["body"] + "\n\n**Cursor leak** too."}, C2]),
                                  "the comment on app/src/main/CartRepository.kt:88 holds #6, which you did not name"),
    "skipping": (DRY.replace(POSTING_LINE, "Skipping 1 finding(s) already posted by an earlier run\n" + POSTING_LINE),
                 "the dry run skips 1 already posted, not 0"),
    "body-section": (DRY.replace(BODY_SECTION, ""), "#5 is neither inline, in the review body nor skipped"),
    "review-body-link": (DRY.replace(SUMMARY, "Two real problems: https://evil.example"),
                         "the review body links to https://evil.example"),
    "exit-1": (CommandResult(1, DRY, "boom\n", False), "the dry run failed: boom"),
    "timed-out": (CommandResult(-9, "", "", True, 300.0), "the dry run failed: timed out after 300 s"),  # C10
    # The Summary line must agree with the payload: the report's Summary is in a body exactly when it says yes
    # (/code-review C7).
    "summary-no-but-in-a-body": (dry_run((1, 2, 5), [C1, C2], summary="no"),
                                 "the dry run says Summary: no, but a review body holds the Summary"),
    "summary-yes-but-in-no-body": (dry_run((1, 2, 5), [C1, C2], body=BODY[BODY.index("### Findings"):]),
                                   "the dry run says Summary: yes, but no review body holds the Summary"),
    "not-a-dry-run": ("not a dry run\n", "the dry run's output was not understood: unknown line: not a dry run"),
    "unknown-line": (DRY.replace(ANCHORS_LINE, ANCHORS_LINE + "Note: anchors cached\n"),
                     "the dry run's output was not understood: unknown line: Note: anchors cached"),
}


@pytest.mark.parametrize("dry, why", list(MISMATCHES.values()), ids=list(MISMATCHES))
async def test_dry_run_mismatch_stops(tmp_path, dry, why):
    rig = Rig(tmp_path, dry=(dry,))
    out = await rig.post()
    assert out == PostOutcome("refused", f"Not posted on #1712: {why}.")
    assert out.text.startswith("Not posted on #1712:")
    assert rig.confirms() == [] and rig.posts() == [] and rig.state() == "done"


async def test_payload_text_checked(tmp_path):
    rig = Rig(tmp_path, dry=(DRY.replace("The list is read before the null check.",
                                         "The list is read before the null check. See https://evil.example"),))
    assert await rig.post() == PostOutcome(
        "refused",
        "Not posted on #1712: the comment on app/src/main/CartRepository.kt:88 links to https://evil.example.")
    assert rig.confirms() == []


NOTHING = (DRY[:DRY.index(POSTING_LINE)] + "Skipping 3 finding(s) already posted by an earlier run\n"
           "Posting 0 comment(s) in 0 review(s)\n")


async def test_nothing_left_to_post(tmp_path):
    rig = Rig(tmp_path, dry=(NOTHING,))
    assert await rig.post() == PostOutcome(
        "nothing", "Nothing left to post on #1712 at 26c4929c5: what you named is already posted.")
    assert rig.confirms() == [] and rig.posts() == [] and rig.state() == "done"

    # An event-only APPROVE whose Summary the ledger has already: the approve without a body is offered.
    event_only = (f"Event  : APPROVE\nSummary: no\nInline : 0 comment(s)\nHEAD   : {HEAD}\n"
                  "Posting 0 comment(s) in 0 review(s)\n")
    rig = Rig(tmp_path / "event-only", dry=(event_only,))
    assert await rig.post(ReplyCommand("post", (), "APPROVE")) == PostOutcome(
        "nothing", "Nothing left to post on #1712 at 26c4929c5: what you named is already posted."
                   ' Reply "approve no summary" to approve without a body.')
    assert rig.confirms() == []


async def test_postcondition_reads_again(tmp_path):
    rig = Rig(tmp_path, reviews=([], [], [], [], [NEW]))
    assert (await rig.post()).state == "posted"
    assert rig.waits == [2.0, 2.0]


def not_shown(why):
    return (f"Posting on #1712 ran, but GitHub does not show it as planned ({why}). Check the PR on GitHub before"
            f" posting again: {PR_URL}")


async def test_postcondition_missing_review(tmp_path):
    # The tool failed before it printed a posted batch: "Not posted" (fix round 1: only then).
    rig = Rig(tmp_path, reviews=([],), confirm=(CommandResult(1, "", "HTTP 422\n", False),))
    out = await rig.post()
    why = "GitHub shows no new review; the tool exited 1: HTTP 422"
    assert out == PostOutcome("failed", f"Not posted on #1712: {why}.")
    assert not out.text.startswith("Posted")
    assert rig.state() == "done"
    [post] = rig.posts()
    assert (post.state, post.landed, post.error) == ("failed", [], why)
    assert rig.waits == [2.0, 2.0] and rig.log.count("reviews") == 5


@pytest.mark.parametrize("confirm, why", [
    (CONFIRMED, "GitHub shows no new review"),  # exit 0
    (CommandResult(1, f"Posted batch 1/1: {NEW.url}\n", "slow\n", False),
     "GitHub shows no new review; the tool exited 1: slow"),
    (CommandResult(-9, "", "", True), "GitHub shows no new review; the tool timed out"),
], ids=["exit-0", "posted-batch-line", "timed-out"])
async def test_tool_reports_posted_but_github_shows_nothing(tmp_path, confirm, why):
    rig = Rig(tmp_path, reviews=([],), confirm=(confirm,))
    out = await rig.post()
    assert out == PostOutcome("unverified", not_shown(why))  # never "Not posted": it may have landed
    [post] = rig.posts()
    assert (post.state, post.landed, post.review_ids, post.error) == ("unverified", [], [], why)
    assert rig.state() == "done"


@pytest.mark.parametrize("new, why", [
    ([review(11, state="COMMENTED")], "a new review is COMMENTED, not APPROVED"),
    ([review(11, commit=NEW_HEAD)], "a new review is for commit 8f01a2b3c"),
])
async def test_new_review_not_as_planned_is_unverified(tmp_path, new, why):
    rig = Rig(tmp_path, reviews=([], [], new))
    assert await rig.post() == PostOutcome("unverified", not_shown(why))  # no batch GitHub shows landed
    [post] = rig.posts()
    assert (post.state, post.landed, post.review_ids) == ("unverified", [], [])
    assert rig.state() == "done"


async def test_postcondition_names_what_differs(tmp_path):
    rig = Rig(tmp_path, reviews=([], [], [review(11, body="Something else")]))
    out = await rig.post()
    assert out.state == "partial"
    assert out.text.endswith("not landed none — the first new review's body is not the planned one.")  # no retry


TWO = dry_run((1, 2, 5), [C1], [C2])  # two batches: #1 with the body (#5), then #2


async def test_partial_batch_failure(tmp_path):
    first = review(11)
    confirm = CommandResult(1, f"Posted batch 1/2: {first.url}\n  app/src/main/CartRepository.kt:88\n",
                            "batch 2: HTTP 422\n", False)
    rig = Rig(tmp_path, dry=(TWO,), confirm=(confirm,), reviews=([], [], [first]), comments={11: COMMENTS[11][:1]})
    out = await rig.post()
    assert out == PostOutcome("partial", (
        f"Partly posted on #1712 at 26c4929c5: batch 1 of 2 landed ({first.url}); landed 1 5; not landed 2"
        " — GitHub shows 1 new review(s), not 2; the tool exited 1: batch 2: HTTP 422."
        '\nReply "post 2 no summary" to post the rest.'))  # batch 1 approved: a second approve is refused (fix
    # round 1), and it carried the Summary (final review F3)
    assert rig.state() == "posted"
    [post] = rig.posts()
    assert (post.state, post.review_ids, post.summary_posted, post.comments) == ("partial", [11], True, 1)
    assert [(item["n"], item["where"]) for item in post.landed] == [(1, "inline"), (5, "body")]
    assert rig.store.posted_keys(SCOPE, N, HEAD) == {
        ("app/src/main/CartRepository.kt", 88, "NPE on an empty list"),
        ("app/src/main/res/values/strings.xml", 212, "Untranslated key?")}


async def test_partial_without_batch_lines_counts_new_reviews(tmp_path):
    first = review(11)
    confirm = CommandResult(1, "", "crashed\n", False)  # the tool died before it printed anything
    rig = Rig(tmp_path, dry=(TWO,), confirm=(confirm,), reviews=([], [], [first]))
    out = await rig.post()
    assert out.state == "partial"
    assert out.text.startswith(f"Partly posted on #1712 at 26c4929c5: batch 1 of 2 landed ({first.url}); landed 1 5;")


async def test_partial_counts_only_what_github_shows(tmp_path):
    first = review(11)
    claimed = f"Posted batch 1/2: {first.url}\nPosted batch 2/2: {PR_URL}#pullrequestreview-12\n"  # exit 0
    rig = Rig(tmp_path, dry=(TWO,), confirm=(claimed,), reviews=([], [], [first]))
    out = await rig.post()
    assert out == PostOutcome("partial", (
        f"Partly posted on #1712 at 26c4929c5: batch 1 of 2 landed ({first.url}); landed 1 5; not landed 2"
        " — GitHub shows 1 new review(s), not 2. The tool also reports batch 2 posted, which GitHub does not"
        ' show: check the PR on GitHub.\nReply "post 2 no summary" to post the rest.'))
    [post] = rig.posts()
    assert (post.review_ids, [item["n"] for item in post.landed]) == ([11], [1, 5])

    # Only reviews by me at the head in the planned state are landed batches; another of mine is not recorded.
    laptop = review(10, state="COMMENTED")
    rig = Rig(tmp_path / "laptop", dry=(TWO,), confirm=(claimed,), reviews=([], [], [laptop, first]))
    out = await rig.post()
    assert out.state == "partial" and "batch 1 of 2 landed" in out.text
    assert rig.posts()[0].review_ids == [11]


@pytest.mark.parametrize("event, flags, retry", [
    ("APPROVE", {"no_summary": True}, "post 2 no summary"),
    ("APPROVE", {"anyway": True}, "post 2 no summary anyway"),  # the Summary landed with batch 1 (F3)
    ("REQUEST_CHANGES", {"no_summary": True, "anyway": True}, "post 2 request changes no summary anyway"),
])
async def test_partial_retry_is_the_same_command(tmp_path, event, flags, retry):
    first = review(11, state=EVENT_STATE[event])
    if flags.get("no_summary"):
        dry = dry_run((1, 2, 5), [C1], [C2], body=BODY[BODY.index("### Findings"):], event=event, summary="no")
    else:
        dry = dry_run((1, 2, 5), [C1], [C2], event=event)
    confirm = CommandResult(1, f"Posted batch 1/2: {first.url}\n", "batch 2: HTTP 422\n", False)
    rig = Rig(tmp_path, dry=(dry,), confirm=(confirm,), reviews=([], [], [first]))
    out = await rig.post(ReplyCommand("post", (1, 2, 5), event, **flags))
    assert out.state == "partial"
    assert out.text.endswith(f'\nReply "{retry}" to post the rest.')
    # The reply posts the rest as the command did (an approval already landed with batch 1), without the Summary,
    # left out or landed.
    assert parse_reply(retry) == ReplyCommand("post", (2,), "COMMENT" if event == "APPROVE" else event,
                                              **{**flags, "no_summary": True})


async def test_partial_retry_leaves_out_a_landed_summary(tmp_path):
    """Batch 1 carries the Summary: once it landed, the rest goes without it (final review F3)."""
    first = review(11)
    confirm = CommandResult(1, f"Posted batch 1/2: {first.url}\n", "batch 2: HTTP 422\n", False)
    rig = Rig(tmp_path, dry=(TWO,), confirm=(confirm,), reviews=([], [], [first]))
    out = await rig.post(ReplyCommand("post", (1, 2, 5), "APPROVE"))
    assert out.state == "partial" and rig.posts()[0].summary_posted
    assert out.text.endswith('\nReply "post 2 no summary" to post the rest.')
    assert parse_reply("post 2 no summary") == ReplyCommand("post", (2,), no_summary=True)


SUMMARY_LANDED = 'The Summary already landed at #1712 at 26c4929c5: add "no summary". Nothing was done.'


async def test_a_landed_summary_is_refused_before_the_confirm_run(tmp_path):
    """The tool's ledger is not the Summary's only never-twice guard: a dry run that carries the Summary where a
    post of Cleave's landed it is refused before the confirm run (final review F3; here the ledger lost it)."""
    changes = review(11, state="CHANGES_REQUESTED", body=SUMMARY)
    rig = Rig(tmp_path, dry=(dry_run((), [], body=SUMMARY, event="REQUEST_CHANGES"),
                             dry_run((1,), [C1], body=SUMMARY, event="REQUEST_CHANGES")),
              confirm=(f"Posted batch 1/1: {changes.url}\n",), reviews=([], [], [changes]), comments={11: []})
    assert (await rig.post(ReplyCommand("post", (), "REQUEST_CHANGES"))).state == "posted"
    assert rig.store.posted_count(SCOPE, N, HEAD) == (0, True)
    assert await rig.post(ReplyCommand("post", (1,), "REQUEST_CHANGES")) == PostOutcome("refused", SUMMARY_LANDED)
    assert rig.log.count("dry") == 2 and len(rig.confirms()) == 1 and len(rig.posts()) == 1


NOTHING_LEFT = "Nothing left to post on #1712 at 26c4929c5: what you named is already posted."


async def test_an_event_only_post_after_its_summary_landed_has_nothing_left(tmp_path):
    """An event-only post names no number: once the Summary its dry run carries has landed, nothing of it is left
    to post. "no summary" would leave an empty review, so the answer is not that (/code-review R2)."""
    changes = review(11, state="CHANGES_REQUESTED", body=SUMMARY)
    event_only = dry_run((), [], body=SUMMARY, event="REQUEST_CHANGES")
    rig = Rig(tmp_path, dry=(event_only, event_only, dry_run((), [], body=SUMMARY, event="APPROVE")),
              confirm=(f"Posted batch 1/1: {changes.url}\n",), reviews=([], [], [changes]), comments={11: []})
    assert (await rig.post(ReplyCommand("post", (), "REQUEST_CHANGES"))).state == "posted"
    assert (await rig.post(ReplyCommand("post", (), "REQUEST_CHANGES"))).text == NOTHING_LEFT
    # An approve can still go without a body, as when the dry run plans nothing.
    assert (await rig.post(ReplyCommand("post", (), "APPROVE"))).text == (
        NOTHING_LEFT + ' Reply "approve no summary" to approve without a body.')
    assert rig.log.count("dry") == 3 and len(rig.confirms()) == 1 and len(rig.posts()) == 1


async def test_unverified_when_github_unreadable(tmp_path):
    rig = Rig(tmp_path, reviews=([], [], ForgeError("GitHub 502")))
    out = await rig.post()
    assert out == PostOutcome("unverified", "Posting on #1712 ran, but GitHub could not be read to check it"
                                            f" (GitHub 502). Check the PR before posting again: {PR_URL}")
    [post] = rig.posts()
    assert (post.state, post.error, post.landed) == ("unverified", "GitHub 502", [])
    assert rig.state() == "done"


async def test_approve_without_summary(tmp_path):
    rig = Rig(tmp_path, reviews=([], [], [APPROVED]), comments={})
    out = await rig.post(APPROVE_ONLY)
    assert out == PostOutcome("posted", f"Approved #1712 at 26c4929c5 without a body: {APPROVED.url}")
    assert rig.forge.approvals == [(1712, HEAD)]
    assert rig.poster.calls == []
    assert not (rig.job_dir / "posting").exists()  # the approve ticks no copy
    [post] = rig.posts()
    assert (post.kind, post.state, post.event, post.numbers, post.review_ids, post.urls) == (
        "approve", "posted", "APPROVE", [], [12], [APPROVED.url])
    assert rig.state() == "posted"
    assert rig.log == ["pr_full", "reviews", "pr_full", "reviews", "approve", "reviews", "review_comments"]


async def test_approve_timeout_checks_github(tmp_path):
    timeout = ForgeError("ReadTimeout", unavailable=True)
    rig = Rig(tmp_path, reviews=([], [], [APPROVED]), comments={}, approve=(timeout,))
    assert await rig.post(APPROVE_ONLY) == PostOutcome(
        "posted", f"Approved #1712 at 26c4929c5 without a body: {APPROVED.url}")
    assert rig.posts()[0].state == "posted" and rig.forge.approvals == [(1712, HEAD)]

    rig = Rig(tmp_path / "none", reviews=([],), comments={}, approve=(timeout,))
    assert await rig.post(APPROVE_ONLY) == PostOutcome(
        "failed", "Not posted on #1712: GitHub said ReadTimeout, and shows no new review.")
    assert rig.posts()[0].state == "failed" and rig.forge.approvals == [(1712, HEAD)]
    assert rig.state() == "done" and rig.waits == [2.0, 2.0]

    rig = Rig(tmp_path / "refused", reviews=([],), comments={}, approve=(ForgeError("GitHub 422"),))
    assert await rig.post(APPROVE_ONLY) == PostOutcome("failed", "Not posted on #1712: GitHub said GitHub 422.")
    assert rig.posts()[0].state == "failed" and rig.forge.approvals == [(1712, HEAD)]
    assert rig.log.count("reviews") == 2  # no post-condition read


async def test_accepted_approve_not_listed_is_unverified(tmp_path):
    rig = Rig(tmp_path, reviews=([],), comments={})  # approve answered with the review; GitHub never lists it
    out = await rig.post(APPROVE_ONLY)
    assert out == PostOutcome("unverified", not_shown("GitHub shows no new review"))
    [post] = rig.posts()
    assert (post.state, post.review_ids) == ("unverified", [])  # the next approve needs "anyway"
    assert rig.state() == "done"


ALREADY_APPROVED = "Already approved #1712 at 26c4929c5. Nothing was done."


async def test_approve_twice_is_refused(tmp_path):
    rig = Rig(tmp_path, reviews=([], [], [APPROVED]), comments={})
    assert (await rig.post(APPROVE_ONLY)).state == "posted"
    rig.forge.script_reviews([APPROVED])  # Cleave's own approval, at the head
    assert await rig.post(APPROVE_ONLY) == PostOutcome("refused", ALREADY_APPROVED)
    assert await rig.post(dataclasses.replace(APPROVE_ONLY, anyway=True)) == PostOutcome("refused", ALREADY_APPROVED)
    # Any command whose event is APPROVE: with numbers, or event-only with the Summary (no "nothing" hint).
    assert await rig.post(dataclasses.replace(CMD, anyway=True)) == PostOutcome("refused", ALREADY_APPROVED)
    assert await rig.post(ReplyCommand("post", (), "APPROVE")) == PostOutcome("refused", ALREADY_APPROVED)
    assert rig.forge.approvals == [(N, HEAD)] and rig.poster.calls == []


async def test_approve_refused_by_cleaves_record(tmp_path):
    rig = Rig(tmp_path, reviews=([],))  # GitHub does not list the approval (yet)
    rig.store.save_post(Post("p0", rig.job.id, SCOPE, N, HEAD, "approve", "posted", T1.isoformat(),
                             event="APPROVE", review_ids=[12]))
    assert await rig.post(APPROVE_ONLY) == PostOutcome("refused", ALREADY_APPROVED)
    assert rig.forge.approvals == []


MAYBE_APPROVED = ('An approve of #1712 at 26c4929c5 may have landed: check the PR, then add "anyway" to your reply to'
                  " approve again. Nothing was done.")


@pytest.mark.parametrize("state, kind", [("unverified", "approve"), ("interrupted", "approve"),
                                         ("interrupted", "review")])
async def test_approve_after_an_unknown_approve_needs_anyway(tmp_path, state, kind):
    rig = Rig(tmp_path, reviews=([], [], [APPROVED]), comments={})  # GitHub lists no approval at first
    rig.store.save_post(Post("p0", rig.job.id, SCOPE, N, HEAD, kind, state, T1.isoformat(), event="APPROVE"))
    assert await rig.post(APPROVE_ONLY) == PostOutcome("refused", MAYBE_APPROVED)
    assert await rig.post(CMD) == PostOutcome("refused", MAYBE_APPROVED)
    assert rig.forge.approvals == [] and rig.poster.calls == []
    # "anyway" retries an approve that truly failed ...
    rig.forge.script_reviews([], [], [APPROVED])
    assert (await rig.post(dataclasses.replace(APPROVE_ONLY, anyway=True))).state == "posted"
    assert rig.forge.approvals == [(N, HEAD)]
    # ... but never past an approval GitHub lists.
    rig = Rig(tmp_path / "listed", reviews=([APPROVED],), comments={})
    rig.store.save_post(Post("p0", rig.job.id, SCOPE, N, HEAD, kind, state, T1.isoformat(), event="APPROVE"))
    assert await rig.post(dataclasses.replace(APPROVE_ONLY, anyway=True)) == PostOutcome("refused", ALREADY_APPROVED)
    assert rig.forge.approvals == []
    # A COMMENT post is not an approve; an unknown approve at another head does not count.
    rig = Rig(tmp_path / "other-head", reviews=([], [], [APPROVED]), comments={})
    rig.store.save_post(Post("p0", rig.job.id, SCOPE, N, NEW_HEAD, kind, state, T1.isoformat(), event="APPROVE"))
    assert (await rig.post(APPROVE_ONLY)).state == "posted"


@pytest.mark.parametrize("cmd", [CMD, APPROVE_ONLY], ids=["review", "approve"])
async def test_approval_seen_right_before_the_post_refuses(tmp_path, cmd):
    laptop = review(7, at=T0 + timedelta(minutes=30), body="")  # approved from the laptop meanwhile
    rig = Rig(tmp_path, reviews=([], [laptop]))
    assert await rig.post(cmd) == PostOutcome("refused", ALREADY_APPROVED)
    assert rig.confirms() == [] and rig.forge.approvals == [] and rig.posts() == []
    # An approval at another head, or someone else's, does not count.
    other = [review(7, commit=NEW_HEAD, body=""), review(8, login="octo-author", body="")]
    rig = Rig(tmp_path / "other", reviews=(other, other, [*other, APPROVED if cmd is APPROVE_ONLY else NEW]),
              comments={**COMMENTS, 12: []})
    assert (await rig.post(dataclasses.replace(cmd, anyway=True))).state == "posted"


@pytest.mark.parametrize("cmd", [CMD, APPROVE_ONLY], ids=["review", "approve"])
async def test_job_outdated_right_before_the_post_refuses(tmp_path, cmd):
    rig = Rig(tmp_path)
    rig.forge.on_reviews = lambda count: count == 2 and rig.store.set_job_state(
        rig.job.id, "superseded", "new head", T1.isoformat())  # during the read right before the post
    assert await rig.post(cmd) == PostOutcome(
        "refused", "Outdated: there is a review of a newer head. Nothing was done.")
    assert rig.confirms() == [] and rig.forge.approvals == [] and rig.posts() == []


@pytest.mark.parametrize("kind", ["posted", "approve", "partial"])
async def test_a_job_superseded_during_the_post_condition_stays_superseded(tmp_path, kind, caplog):
    """A post that landed sets its job `posted` only while the job is still done or posted: a newer head polled
    while GitHub was read after the confirm run leaves the job superseded (final review M1)."""
    if kind == "posted":
        rig, cmd = Rig(tmp_path), CMD
    elif kind == "approve":
        rig, cmd = Rig(tmp_path, reviews=([], [], [APPROVED]), comments={}), APPROVE_ONLY
    else:
        first = review(11)
        confirm = CommandResult(1, f"Posted batch 1/2: {first.url}\n", "batch 2: HTTP 422\n", False)
        rig, cmd = Rig(tmp_path, dry=(TWO,), confirm=(confirm,), reviews=([], [], [first])), CMD
    rig.forge.on_reviews = lambda count: count == 3 and rig.store.set_job_state(
        rig.job.id, "superseded", "new head", T1.isoformat())  # the first read after the confirm run
    with caplog.at_level(logging.INFO, logger="orchestrator.reviewer.posting"):
        out = await rig.post(cmd)
    assert out.state == ("partial" if kind == "partial" else "posted")
    assert rig.posts()[0].state == out.state
    job = rig.store.job(rig.job.id)
    assert (job.state, job.reason) == ("superseded", "new head")
    assert f"Job {rig.job.id} is superseded now: left as it is after the post" in caplog.text


@pytest.mark.parametrize("cmd", [ReplyCommand("text", (1,)), ReplyCommand("post_replies", (1,)),
                                 ReplyCommand("review_again"), ReplyCommand("edit", (1,), instruction="x")],
                         ids=["text", "post_replies", "review_again", "edit"])
async def test_refuses_a_command_that_is_not_a_post(tmp_path, cmd):
    rig = Rig(tmp_path)
    assert await rig.post(cmd) == PostOutcome("refused", "Not a post command. Nothing was done.")
    assert rig.log == [] and rig.posts() == []


async def test_no_summary_dry_run_with_a_summary_stops(tmp_path):
    rig = Rig(tmp_path)  # DRY says "Summary: yes" and its body opens with the Summary
    assert await rig.post(dataclasses.replace(CMD, no_summary=True)) == PostOutcome(
        "refused", "Not posted on #1712: the dry run posts the Summary, which you left out.")
    assert rig.confirms() == []


async def test_held_with_nonzero_exit_is_posted(tmp_path):
    rig = Rig(tmp_path, confirm=(CommandResult(1, f"Posted batch 1/1: {NEW.url}\n", "warning: slow\n", False),))
    out = await rig.post()
    assert out.state == "posted"
    assert out.text.startswith(f"Posted on #1712 at 26c4929c5 — APPROVE, 1 review: {NEW.url}\n")
    assert out.text.split("\n")[-1] == "The tool exited 1, but GitHub shows everything posted."
    assert rig.state() == "posted" and rig.posts()[0].state == "posted"


async def test_a_confirm_output_not_understood_is_checked_on_github(tmp_path):
    """A confirm run whose output Cleave does not understand still reaches the post-condition: what GitHub shows
    is recorded as landed, never "not understood" with nothing recorded (/code-review C2)."""
    rig = Rig(tmp_path, confirm=(f"Posted batch 1/1: {NEW.url}\nWARNING: rate limited\n",))
    out = await rig.post()
    assert out.state == "posted" and out.text.startswith(f"Posted on #1712 at 26c4929c5 — APPROVE, 1 review: {NEW.url}")
    assert out.text.split("\n")[-1] == ("The tool's output was not understood (unknown line: WARNING: rate"
                                        " limited), but GitHub shows everything posted.")
    [post] = rig.posts()
    assert (post.state, post.review_ids, [item["n"] for item in post.landed]) == ("posted", [11], [1, 2, 5])
    assert rig.state() == "posted"
    assert rig.log[rig.log.index("confirm"):] == ["confirm", "reviews", "review_comments"]

    # Part of it on GitHub: partly posted, only what GitHub shows landed.
    first = review(11)
    confirm = CommandResult(1, f"Posted batch 1/2: {first.url}\nERROR: batch 2 refused\n", "HTTP 422\n", False)
    rig = Rig(tmp_path / "partial", dry=(TWO,), confirm=(confirm,), reviews=([], [], [first]))
    out = await rig.post()
    assert out.state == "partial"
    assert out.text.startswith(f"Partly posted on #1712 at 26c4929c5: batch 1 of 2 landed ({first.url}); landed 1 5;"
                               " not landed 2 — GitHub shows 1 new review(s), not 2; the tool exited 1: HTTP 422; the"
                               " tool's output was not understood (unknown line: ERROR: batch 2 refused).")
    assert (rig.posts()[0].review_ids, [item["n"] for item in rig.posts()[0].landed]) == ([11], [1, 5])

    # Nothing new on GitHub: it may still have landed, so never "Not posted", whatever the exit.
    rig = Rig(tmp_path / "nothing", reviews=([],), confirm=(CommandResult(1, "Traceback\n", "boom\n", False),))
    why = ("GitHub shows no new review; the tool exited 1: boom; the tool's output was not understood (unknown line:"
           " Traceback)")
    assert await rig.post() == PostOutcome("unverified", not_shown(why))
    [post] = rig.posts()
    assert (post.state, post.landed, post.error) == ("unverified", [], why)
    assert rig.state() == "done"


@pytest.mark.parametrize("cmd", [CMD, APPROVE_ONLY], ids=["review", "approve"])
@pytest.mark.parametrize("heads, reviews", [
    ((UNAVAILABLE,), ([],)),
    ((HEAD,), (UNAVAILABLE,)),
    ((HEAD, UNAVAILABLE), ([],)),
    ((HEAD,), ([], UNAVAILABLE)),
], ids=["first-pr-read", "already-reviewed-read", "second-pr-read", "before-read"])
async def test_github_unreadable_before_confirm(tmp_path, heads, reviews, cmd):
    rig = Rig(tmp_path, heads=heads, reviews=reviews)
    assert await rig.post(cmd) == PostOutcome(
        "refused", "GitHub could not be read for #1712 (GitHub 502): nothing posted.")
    assert rig.confirms() == [] and rig.forge.approvals == [] and rig.posts() == [] and rig.state() == "done"


async def test_posts_run_one_at_a_time(tmp_path):
    # One approval at a head; the Summary landed with the first post, so the tool's ledger leaves it out of the second.
    first, second = review(11, body=SUMMARY), review(12, state="COMMENTED", body=FINDINGS)
    rig = Rig(tmp_path, dry=(dry_run((1, 2), [C1, C2], body=SUMMARY),
                             dry_run((5,), [], body=FINDINGS, event="COMMENT", summary="no")),
              confirm=(f"Posted batch 1/1: {first.url}\n", f"Posted batch 1/1: {second.url}\n"),
              reviews=([], [], [first], [first], [first], [first, second]),
              comments={11: COMMENTS[11], 12: []}, dry_wait=0.05)
    one, two = await asyncio.gather(rig.post(ReplyCommand("post", (1, 2), "APPROVE")),
                                    rig.post(ReplyCommand("post", (5,))))
    assert (one.state, two.state) == ("posted", "posted")
    # The second post reads GitHub only after the first's post-condition: its dry run starts after that confirm.
    assert rig.log == ["pr_full", "reviews", "dry", "pr_full", "reviews", "confirm", "reviews", "review_comments"] * 2


async def test_same_post_gathered_twice(tmp_path):
    rig = Rig(tmp_path, dry_wait=0.05)
    one, two = await asyncio.gather(rig.post(), rig.post())
    assert one.state == "posted"
    assert two == PostOutcome("refused", "#1 is already posted at this head. Nothing was done.")
    assert [confirm for _, confirm in rig.poster.calls] == [False, True]


async def test_report_copy_problems_refused(tmp_path):
    rig = Rig(tmp_path)
    (rig.job_dir / NAME).unlink()
    assert await rig.post() == PostOutcome("refused", "Not posted on #1712: the job's report file is missing.")

    rig = Rig(tmp_path / "planted")
    (rig.job_dir / "posting").write_text("not Cleave's")
    assert await rig.post() == PostOutcome(
        "refused", "Not posted on #1712: the job copy's posting directory is not Cleave's.")

    rig = Rig(tmp_path / "odd")
    (rig.job_dir / NAME).write_text(REPORT.replace(SUMMARY, SUMMARY + chr(0x2029)), encoding="utf-8")
    assert await rig.post() == PostOutcome(
        "refused", "Not posted on #1712: the job's report holds a character that tools read differently (U+2029).")
    assert rig.poster.calls == []


class BusyClone(ReviewClone):
    @asynccontextmanager
    async def reader(self, *, wait=600.0, step=2.0):
        raise CloneError("the review clone is in use; try again in a minute")
        yield


async def test_clone_in_use_refused(tmp_path):
    rig = Rig(tmp_path, clone=BusyClone(tmp_path / "clone", "file:///nowhere", "develop", "ghp_not_real"))
    assert await rig.post() == PostOutcome(
        "refused", "Not posted on #1712: the review clone is in use; try again in a minute.")
    assert rig.poster.calls == [] and rig.posts() == []


class ChangingClone(ReviewClone):
    """In a job's session phase whose git files change while the dry run runs."""

    def __init__(self, *args):
        super().__init__(*args)
        self.changed = []

    def changed_git_files(self):
        return self.changed


async def test_git_config_changed_before_confirm_refused(tmp_path):
    clone = ChangingClone(tmp_path / "clone", "file:///nowhere", "develop", "ghp_not_real")
    rig = Rig(tmp_path, clone=clone)
    rig.poster.on_dry = lambda: setattr(clone, "changed", [clone.path / ".git" / "config"])
    assert await rig.post() == PostOutcome(
        "refused", "Not posted on #1712: the review session changed the clone's git config;"
                   " posting waits until the job ends.")
    assert rig.confirms() == [] and rig.posts() == []


@pytest.mark.parametrize("where", ["confirm", "approve"])
async def test_cancelled_post_is_interrupted(tmp_path, where):
    if where == "confirm":
        rig, cmd = Rig(tmp_path, confirm=(asyncio.CancelledError(),)), CMD
    else:
        rig, cmd = Rig(tmp_path, approve=(asyncio.CancelledError(),)), APPROVE_ONLY
    with pytest.raises(asyncio.CancelledError):
        await rig.post(cmd)
    # A cancel is a shutdown, as a rule: the Post stays `posting`, neither failed nor "Not posted", and the
    # restart's recover() marks it interrupted, for the line Task 14 sends.
    [post] = rig.posts()
    assert (post.state, post.error) == ("posting", "") and rig.state() == "done"
    [recovered] = rig.posting.recover()
    assert (recovered.id, recovered.state, recovered.updated_at) == (post.id, "interrupted", T1.isoformat())
    assert rig.posts()[0].state == "interrupted" and rig.posting.recover() == []
    assert interrupted_line(SCOPE, N) == ('[acme/app] #1712: a post was interrupted by a restart. Check the PR, then'
                                          ' reply "post …" again with "anyway": what landed is skipped.')


async def test_cancelled_before_the_post_leaves_nothing(tmp_path):
    rig = Rig(tmp_path, dry=(asyncio.CancelledError(),))
    with pytest.raises(asyncio.CancelledError):
        await rig.post()
    assert rig.posts() == [] and rig.confirms() == []
    rig.poster.dry = Script(DRY)
    rig.forge.script_reviews([], [], [NEW])
    assert (await rig.post()).state == "posted"  # the lock and the lease were let go


async def test_recover_marks_posting_posts_interrupted(tmp_path):
    rig = Rig(tmp_path)
    rig.store.save_post(Post("p1", rig.job.id, SCOPE, N, HEAD, "review", "posting", T0.isoformat()))
    rig.store.save_post(Post("p2", rig.job.id, SCOPE, N, HEAD, "review", "posted", T0.isoformat()))
    [recovered] = rig.posting.recover()
    assert (recovered.id, recovered.state, recovered.updated_at) == ("p1", "interrupted", T1.isoformat())
    assert {p.id: p.state for p in rig.posts()} == {"p1": "interrupted", "p2": "posted"}
    assert rig.posting.recover() == []


INTERNAL_AFTER = ("Posting on #1712 ran, but stopped on an internal error ({}): it may have landed. Check the PR"
                  " before posting again.")


async def test_internal_error_never_raises(tmp_path):
    rig = Rig(tmp_path, comments={11: RuntimeError("boom")})  # in the post-condition: after the confirm run
    assert await rig.post() == PostOutcome("unverified", INTERNAL_AFTER.format("RuntimeError"))
    [post] = rig.posts()
    assert (post.state, post.error) == ("unverified", "internal error (RuntimeError)") and rig.state() == "done"
    # Before the confirm run nothing was posted: failed, and no Post.
    rig = Rig(tmp_path / "before", reviews=(RuntimeError("boom"),))
    assert await rig.post() == PostOutcome(
        "failed", "Posting stopped on an internal error (RuntimeError); check the PR before posting again.")
    assert rig.posts() == [] and rig.confirms() == []


async def test_an_internal_error_once_the_post_ran_is_unverified(tmp_path, monkeypatch):
    """Once the confirm run (or the approve) ran, it may have landed: an internal error leaves the Post unverified,
    never failed, and an approve at that head then needs "anyway" (/code-review C4)."""
    def broken(*args):
        raise KeyError("n")

    rig = Rig(tmp_path)
    monkeypatch.setattr(posting_module, "_landed", broken)  # GitHub showed the post; recording it fails
    assert await rig.post() == PostOutcome("unverified", INTERNAL_AFTER.format("KeyError"))
    [post] = rig.posts()
    assert (post.state, post.error, post.event) == ("unverified", "internal error (KeyError)", "APPROVE")
    assert rig.state() == "done" and len(rig.confirms()) == 1
    rig.forge.script_reviews([])  # GitHub does not list the approval (yet)
    assert await rig.post(APPROVE_ONLY) == PostOutcome("refused", MAYBE_APPROVED)
    rig.forge.script_reviews([], [], [APPROVED])
    assert (await rig.post(dataclasses.replace(APPROVE_ONLY, anyway=True))).state == "posted"

    # A Post saved in its final state stays so: the error came after it (here, in the job's state write).
    monkeypatch.undo()
    rig = Rig(tmp_path / "after-the-record")
    monkeypatch.setattr(rig.store, "set_job_state", broken)
    assert await rig.post() == PostOutcome("unverified", INTERNAL_AFTER.format("KeyError"))
    assert rig.posts()[0].state == "posted"


async def test_a_poll_during_the_post_condition_leaves_the_job_to_the_post(tmp_path):
    """The post takes the operator's review turn away. A poll that sees the turn gone while GitHub is read after
    the confirm run (the Post still `posting`) does not stale the job, and the post ends it posted (/code-review
    C3)."""
    from orchestrator.reviewer.service import ReviewerService
    from orchestrator.switches import Switches
    from tests.unit.test_reviewer_runner import board_row
    from tests.unit.test_reviewer_service import FakeClone, FakeReadiness, det, snap, spr
    from tests.unit.test_reviewer_service import FakeForge as BoardForge

    rig = Rig(tmp_path, reviews=([], [], [], [NEW]))  # the first read after the confirm run shows nothing yet
    rig.store.replace_board(SCOPE, {N: board_row(HEAD)})  # the review turn, as the last poll saw it
    board = RepoContext("acme", "app", ReviewerConfig(enabled=True), FakeClone(tmp_path / "board"),
                        FakeReadiness(snap(spr(N, []))), BoardForge({N: det(N, head=HEAD)}))
    service = ReviewerService([board], rig.store, Switches(connect(tmp_path / "rv" / "reviewer.db")),
                              lambda key: False, None, lambda project_id, repo_id: set(), now=lambda: T1)
    seen = []

    async def poll(seconds):  # between the post-condition's reads
        await service.poll(board)
        seen.append(rig.state())

    rig.posting = Posting(rig.store, ledgers_dir=rig.ledgers, now=lambda: T1, sleep=poll)
    assert (await rig.post()).state == "posted"
    assert seen == ["done"] and rig.store.board(SCOPE)[N].my_turns == []  # the poll saw the turn gone
    assert rig.state() == "posted"


@pytest.mark.parametrize("stamp, reason, state", [
    (T1, "turn gone", "posted"),  # staled once the post had started
    (T0, "turn gone", "stale"),  # stamped before it started
    (T1, "PR gone", "stale"),
])
async def test_a_post_that_landed_ends_a_job_its_turn_staled(tmp_path, stamp, reason, state, caplog):
    """A job staled "turn gone" after the post started is set posted when the post landed; any other stale stays
    (/code-review C3)."""
    rig = Rig(tmp_path)
    rig.forge.on_reviews = lambda count: count == 3 and rig.store.set_job_state(
        rig.job.id, "stale", reason, stamp.isoformat())  # the first read after the confirm run
    with caplog.at_level(logging.INFO, logger="orchestrator.reviewer.posting"):
        assert (await rig.post()).state == "posted"
    assert rig.state() == state
    assert (f"Job {rig.job.id} is stale now: left as it is after the post" in caplog.text) == (state == "stale")


def test_check_dry_run_pure():
    plan = check_dry_run(parse_review_dry_run(DRY), parse_report(REPORT, HEAD), (1, 2, 5), "APPROVE", HEAD, ALLOWED)
    assert plan == DryRunPlan(1, 2, BODY, True, (1, 2), (2,), (5,), (), ((1, 2, 5),))
    two = check_dry_run(parse_review_dry_run(TWO), parse_report(REPORT, HEAD), (1, 2, 5), "APPROVE", HEAD, ALLOWED)
    assert two.batches == ((1, 5), (2,))
    assert EVENT_STATE == {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}


def check(text, report=REPORT, **kwargs):
    return check_dry_run(parse_review_dry_run(text), parse_report(report, HEAD), (1, 2, 5), "APPROVE", HEAD, ALLOWED,
                         **kwargs)


FINDINGS = BODY[BODY.index("### Findings"):]  # the body without the Summary


def test_check_dry_run_reads_the_body_as_two_parts():
    # An unnamed item's bold title: refused in the findings part, and in the Summary part unless the report's
    # own Summary holds it.
    cursor = "**Cursor leak**"
    assert check(dry_run((1, 2, 5), [C1, C2], body=f"{BODY}\n\n{cursor} `x.kt:1`")) == (
        "the review body holds #6, which you did not name")
    assert check(dry_run((1, 2, 5), [C1, C2], body=f"{cursor} too. {BODY}")) == (
        "the review body holds #6, which you did not name")
    own = REPORT.replace(SUMMARY, f"{SUMMARY} {cursor} is older.")
    assert isinstance(check(dry_run((1, 2, 5), [C1, C2], body=BODY.replace(SUMMARY, f"{SUMMARY} {cursor} is older.")),
                            report=own), DryRunPlan)
    # A named item is in the body only in the findings part, not because the Summary bolds its title.
    assert check(dry_run((1, 2, 5), [C1, C2], body="**Untranslated key?** is in the Summary.")) == (
        "#5 is neither inline, in the review body nor skipped")
    # "no summary": neither the Summary line nor the body may carry one.
    assert check(DRY, no_summary=True) == "the dry run posts the Summary, which you left out"
    assert check(dry_run((1, 2, 5), [C1, C2], summary="no"), no_summary=True) == (
        "the dry run posts the Summary, which you left out")
    assert check(dry_run((1, 2, 5), [C1, C2], body=FINDINGS, summary="no"), no_summary=True).in_body == (5,)


def test_check_dry_run_matches_an_items_text():
    other = {**C1, "body": "**NPE on an empty list**\n\nSomething else entirely."}
    assert check(dry_run((1, 2, 5), [other, C2])) == (
        "the comment on app/src/main/CartRepository.kt:88 matches no named number")
    wrapped = {**C1, "body": "**NPE on an empty list**\n\nThe list is read\nbefore the null check."}
    assert check(dry_run((1, 2, 5), [wrapped, C2])).inline == (1, 2)  # the tool may wrap the text
    # A body item's own text must be in the findings part too.
    with_text = REPORT.replace("(outside the diff - goes to the review body)\n",
                               "(outside the diff - goes to the review body)\nCheck the key.\n")
    assert check(DRY, report=with_text) == "#5 is neither inline, in the review body nor skipped"
    assert check(dry_run((1, 2, 5), [C1, C2], body=f"{BODY} Check the key."), report=with_text).in_body == (5,)


def test_only_posting_confirms():
    files = [*(path for top in ("orchestrator", "integrations", "dashboard") for path in (ROOT / top).rglob("*.py")),
             ROOT / "main.py"]
    posts = re.compile(r"confirm\s*=\s*True|\.approve\s*\(")
    found = {path.relative_to(ROOT).as_posix() for path in files if posts.search(path.read_text(encoding="utf-8"))}
    assert found == {"orchestrator/reviewer/posting.py"}


REPORT_CHANGED = "The report changed while posting (an edit landed): look at it again and reply. Nothing was done."


def land(rig, kind="edit"):
    """What an edit (of 2) or a draft (of 7) job does at its end: the copy's base report and the run's report
    rewritten together."""
    base = ensure_base(rig.job_dir, NAME)
    run = rig.store.run(rig.job.id)
    text, drafted = base.read_bytes().decode("utf-8"), frozenset(run.drafted)
    if kind == "edit":
        new = apply_edit(text, HEAD, 2, "Use exponential backoff.", drafted)
    else:
        new = apply_draft(text, HEAD, 7, "Null check", "app/src/main/Adapter.kt", 12, "Is this reachable?", drafted)
        drafted |= {7}
        run.drafted.append(7)
    write_atomic(base, new)
    run.report = parse_report(new, HEAD, drafted).to_dict()
    rig.store.save_run(run)


async def test_edit_landing_after_the_report_was_checked_is_refused(tmp_path):
    """Between step 1 (the report read and checked) and step 5 (the copy ticked): no dry run, no confirm."""
    rig = Rig(tmp_path)
    rig.forge.on_reviews = lambda count: count == 1 and land(rig)
    assert await rig.post() == PostOutcome("refused", REPORT_CHANGED)
    assert rig.poster.calls == [] and rig.posts() == [] and rig.state() == "done"


async def test_edit_landing_during_the_dry_run_is_refused(tmp_path):
    rig = Rig(tmp_path)
    rig.poster.on_dry = lambda: land(rig)
    assert await rig.post() == PostOutcome("refused", REPORT_CHANGED)
    assert rig.log.count("dry") == 1 and rig.confirms() == [] and rig.posts() == [] and rig.state() == "done"


async def test_draft_landing_during_the_post_leaves_the_named_items(tmp_path):
    """A draft adds an unnamed item: the named ones are as checked, and the post goes on."""
    rig = Rig(tmp_path)
    rig.poster.on_dry = lambda: land(rig, "draft")
    assert (await rig.post()).state == "posted"
    assert len(rig.confirms()) == 1
