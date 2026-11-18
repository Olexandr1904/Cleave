# tests/unit/test_reviewer_store.py
from __future__ import annotations
import dataclasses
import json
import pytest
from orchestrator.reviewer.db import MIGRATIONS, connect
from orchestrator.reviewer.models import Anchor, BoardRow, Job, JobRun, Post, RepoState
from orchestrator.reviewer.store import ReviewerStore

T0, T1 = "2026-11-20T10:00:00+00:00", "2026-11-20T10:05:00+00:00"

def recheck_of(job_id):
    """A re-check job of PR 1712 in acme/app, as posted_replies reads one."""
    return Job(job_id, "acme/app", 1712, "recheck", "b" * 40, f"recheck:{job_id}", "done", "", T1, T1)

def row(n=1712, head="a" * 40, actions=("review",)):
    return BoardRow(n, "Fix cart sync ACME-1234", f"https://github.com/acme-org/acme-app/pull/{n}", "octo-author",
                    ["reviewer"], head, T0, False, 14, [{"action": a, "threads": []} for a in actions], [], False,
                    False, "ACME-1234", T0, T0)

@pytest.fixture
def store(tmp_path):
    return ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))

def test_migrations_applied(tmp_path):
    conn = connect(tmp_path / "r.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 6

def test_board_roundtrip_and_replace(store):
    store.replace_board("acme/app", {1712: row(), 1720: row(1720)})
    assert store.board("acme/app")[1712] == row()
    assert store.board("acme/app")[1712].my_actions == ["review"]
    store.replace_board("acme/app", {1720: row(1720)})
    assert list(store.board("acme/app")) == [1720]
    assert store.board("other/repo") == {}

def test_repo_state_default_and_save(store):
    assert store.repo_state("acme/app") == RepoState("acme/app")
    store.save_repo_state(RepoState("acme/app", True, "ok", "", 0, T1, T1, T1))
    assert store.repo_state("acme/app").first_poll_done

def test_jobs(store):
    j = store.add_job("acme/app", 1712, "review", "a" * 40, "a" * 40, T0)
    assert (j.state, j.reason, len(j.id)) == ("queued", "", 12)
    store.add_job("acme/app", 1712, "recheck", "a" * 40, "k", T1)
    assert [x.kind for x in store.jobs("acme/app", 1712)] == ["recheck", "review"]
    store.set_job_state(j.id, "cancelled", "turn gone", T1)
    assert store.jobs("acme/app", 1712)[1].state == "cancelled"
    with pytest.raises(ValueError):
        store.set_job_state(j.id, "exploded", "", T1)

def test_skips_and_notices(store):
    store.skip("acme/app", 1712, "a" * 40, T0)
    assert store.is_skipped("acme/app", 1712, "a" * 40) and not store.is_skipped("acme/app", 1712, "b" * 40)
    assert not store.notice_sent("acme/app", 1712, "large", "a" * 40)
    store.mark_notice("acme/app", 1712, "large", "a" * 40, T0)
    assert store.notice_sent("acme/app", 1712, "large", "a" * 40)
    store.clear_notice("acme/app", 1712, "large")
    assert not store.notice_sent("acme/app", 1712, "large", "a" * 40)

def test_skipped_at_is_the_last_skip(store):
    assert store.skipped_at("acme/app", 1712, "a" * 40) == ""
    store.skip("acme/app", 1712, "a" * 40, T0)
    assert store.skipped_at("acme/app", 1712, "a" * 40) == T0
    store.skip("acme/app", 1712, "a" * 40, T1)   # skipped again: a job queued in between is skipped too
    assert store.skipped_at("acme/app", 1712, "a" * 40) == T1
    assert store.skipped_at("acme/app", 1712, "b" * 40) == ""

def test_board_row_ignores_unknown_stored_keys():
    data = json.loads(row().to_json()) | {"future_field": 1}
    assert BoardRow.from_json(json.dumps(data)) == row()

def test_repo_state_ignores_unknown_stored_keys(tmp_path):
    conn = connect(tmp_path / "r.db")
    data = dataclasses.asdict(RepoState("acme/app", True, "ok")) | {"future_field": 1}
    conn.execute("INSERT INTO repo_state (scope, data) VALUES (?, ?)", ("acme/app", json.dumps(data)))
    assert ReviewerStore(conn).repo_state("acme/app") == RepoState("acme/app", True, "ok")

def test_retry_at_and_runnable(store):
    a = store.add_job("acme/app", 1, "review", "a" * 40, "a" * 40, T0)
    b = store.add_job("acme/app", 2, "recheck", "b" * 40, "k", T1)
    store.add_job("acme/app", 3, "edit", "c" * 40, "x", T1)
    store.set_job_state(b.id, "deferred", "usage limit", T1, retry_at="2026-11-20T11:00:00+00:00")
    assert store.job(b.id).retry_at == "2026-11-20T11:00:00+00:00"
    assert [j.id for j in store.runnable_jobs("2026-11-20T10:30:00+00:00")] == [a.id]
    assert [j.id for j in store.runnable_jobs("2026-11-20T11:00:00+00:00")] == [a.id, b.id]
    store.set_job_state(a.id, "running", "", T1)
    assert [j.id for j in store.jobs_in_state("running")] == [a.id]
    assert store.job("nope") is None

def test_job_run_roundtrip_and_unsent(store):
    j = store.add_job("acme/app", 1, "review", "a" * 40, "a" * 40, T0)
    run = JobRun(j.id, started_at=T0, cost_usd=2.5, report={"items": []}, tg_lines={"1": "x"})
    store.save_run(run)
    assert store.run(j.id) == run
    assert store.done_unsent() == []
    store.set_job_state(j.id, "done", "", T1)
    assert store.done_unsent() == [(store.job(j.id), run)]
    run.message_sent = True
    store.save_run(run)
    assert store.done_unsent() == []

def test_job_run_send_attempts_default_for_old_rows(store):
    j = store.add_job("acme/app", 1, "review", "a" * 40, "a" * 40, T0)
    store._conn.execute("INSERT INTO job_runs (job_id, data) VALUES (?, ?)",
                        (j.id, json.dumps({"job_id": j.id, "message_sent": False})))
    run = store.run(j.id)
    assert (run.send_attempts, run.last_attempt_at) == (0, "")
    run.send_attempts, run.last_attempt_at = 3, T1
    store.save_run(run)
    assert (store.run(j.id).send_attempts, store.run(j.id).last_attempt_at) == (3, T1)

def test_prepare_hash(store):
    assert store.prepare_hash("acme/app") == ""
    store.set_prepare_hash("acme/app", "h1", T0)
    store.set_prepare_hash("acme/app", "h2", T1)
    assert store.prepare_hash("acme/app") == "h2"

def test_repo_state_me_defaults_for_old_rows(store):
    store._conn.execute("INSERT INTO repo_state (scope, data) VALUES (?, ?)", ("acme/app", '{"scope": "acme/app"}'))
    assert store.repo_state("acme/app").me == ""

def test_anchor_targets(store):
    store.add_anchor("42", 1, "j1", T0)
    store.add_anchor("42", 2, "", T0, scope="acme/app", number=1712)
    assert store.anchor("42", 1) == Anchor("j1", "", 0)
    assert store.anchor("42", 2) == Anchor("", "acme/app", 1712)
    assert store.anchor("42", 3) is None

def test_posts_and_what_landed(store):
    a, b = "a" * 40, "b" * 40
    landed = [{"n": 1, "path": "a.kt", "line": 88, "title": "NPE", "where": "inline"},
              {"n": 5, "path": "s.xml", "line": 212, "title": "Key?", "where": "body"}]
    p1 = Post("p1", "j1", "acme/app", 1712, a, "review", "posted", T0, T0, numbers=[1, 5], event="APPROVE",
              landed=landed, summary_posted=True, comments=1, review_ids=[11], urls=["u11"])
    store.save_post(p1)
    store.save_post(Post("p2", "j1", "acme/app", 1712, a, "review", "failed", T1, T1, numbers=[2],
                         landed=[{"n": 2, "path": "b.kt", "line": 1, "title": "X", "where": "inline"}]))
    store.save_post(Post("p3", "j2", "acme/app", 1712, b, "replies", "posted", T1, T1,
                         landed=[{"n": 3, "path": "", "line": None, "title": "", "where": "comment"}]))
    assert store.post("p1") == p1 and [p.id for p in store.posts("acme/app", 1712)] == ["p3", "p2", "p1"]
    assert store.posted_keys("acme/app", 1712, a) == {("a.kt", 88, "NPE"), ("s.xml", 212, "Key?")}
    assert store.posted_count("acme/app", 1712, a) == (1, True) and store.posted_count("acme/app", 1712, b) == (0, False)
    assert store.cleave_review_ids("acme/app", 1712) == {11}
    assert store.posted_replies(recheck_of("j2")) == {3} and [p.id for p in store.posts_in_state("failed")] == ["p2"]

def test_posted_replies_counts_what_github_showed_in_any_final_post(store):
    def reply(post_id, state, n, where="reply", job="j2"):
        store.save_post(Post(post_id, job, "acme/app", 1712, "b" * 40, "replies", state, T1,
                             landed=[{"n": n, "path": "", "line": None, "title": "", "where": where}]))

    for post_id, (state, n) in enumerate([("posted", 1), ("partial", 2), ("failed", 3), ("unverified", 4)]):
        reply(f"p{post_id}", state, n)
    reply("p5", "unverified", 5, where="resolve")  # a resolve is not a reply
    reply("p6", "interrupted", 6)  # Cleave never records what an interrupted post landed
    reply("p7", "posting", 7)
    reply("p8", "posted", 8, job="j3")
    assert store.posted_replies(recheck_of("j2")) == {1, 2, 3, 4}

def test_posted_replies_reads_the_prs_posts(store, monkeypatch):
    """The job's PR's posts, filtered by the job, never a scan of the whole table; or the posts given (/code-review
    C15)."""
    recheck = store.add_job("acme/app", 1712, "recheck", "b" * 40, "k", T1)
    store.save_post(Post("p1", recheck.id, "acme/app", 1712, "b" * 40, "replies", "posted", T1,
                         landed=[{"n": 3, "path": "", "line": None, "title": "", "where": "reply"}]))
    reads, real = [], store.posts
    monkeypatch.setattr(store, "posts", lambda scope, number: reads.append((scope, number)) or real(scope, number))
    assert store.posted_replies(recheck) == {3} and reads == [("acme/app", 1712)]
    assert store.posted_replies(recheck, []) == set() and len(reads) == 1

def test_posted_numbers_is_the_one_already_posted_test(store):
    # Final review M8: replies, posting and the dashboard ask this one helper which numbers landed.
    from orchestrator.reviewer.report import parse_report
    from tests.unit.test_reviewer_report import HEAD, REPORT
    report = parse_report(REPORT, HEAD)
    first, second = report.items[0], report.items[1]
    review = store.add_job("acme/app", 1712, "review", HEAD, HEAD, T0)
    recheck = store.add_job("acme/app", 1712, "recheck", HEAD, f"recheck:{HEAD}", T1)
    at = lambda item, n=99: [{"n": n, "path": item.path, "line": item.line, "title": item.title, "where": "inline"}]
    store.save_post(Post("p1", "j-earlier", "acme/app", 1712, HEAD, "review", "posted", T0, landed=at(first)))
    store.save_post(Post("p2", review.id, "acme/app", 1712, "b" * 40, "review", "posted", T0, landed=at(second)))
    store.save_post(Post("p3", recheck.id, "acme/app", 1712, HEAD, "replies", "partial", T1,
                         landed=[{"n": 3, "path": "", "line": None, "title": "", "where": "reply"}]))
    assert store.posted_numbers(review, report) == {first.number}  # by place and title at the job's head
    assert store.posted_numbers(review, None) == set()
    assert store.posted_numbers(recheck, None) == store.posted_replies(recheck) == {3}

def test_posted_count_counts_review_comments_only(store):
    a = "a" * 40
    store.save_post(Post("p1", "j1", "acme/app", 1712, a, "review", "posted", T0, comments=3))
    store.save_post(Post("p2", "j2", "acme/app", 1712, a, "replies", "posted", T1, comments=2,
                         summary_posted=True))
    assert store.posted_count("acme/app", 1712, a) == (3, False)   # a re-check's replies are not counted

def test_confirmation_claimed_once(store):
    c = store.add_confirmation("j1", "42", {"action": "post", "numbers": [1]}, T0)
    claimed = store.claim_confirmation(c.id, "confirmed")
    assert (claimed.state, claimed.command) == ("confirmed", {"action": "post", "numbers": [1]})
    assert store.claim_confirmation(c.id, "confirmed") is None and store.claim_confirmation("nope", "declined") is None

def test_confirmation_read_claims_nothing(store):
    c = store.add_confirmation("j1", "42", {"action": "post", "numbers": [1]}, T0)
    assert store.confirmation(c.id) == c and store.confirmation("nope") is None
    assert store.claim_confirmation(c.id, "declined").state == "declined"
    assert store.confirmation(c.id).state == "declined"

def test_small_jobs_and_unsent_kinds(store):
    review = store.add_job("acme/app", 1, "review", "a" * 40, "a" * 40, T0)
    edit = store.add_job("acme/app", 1, "edit", "a" * 40, f"{review.id}:2", T1)
    store.add_job("acme/app", 1, "draft", "a" * 40, f"{review.id}:7", T1)
    assert [j.kind for j in store.small_jobs()] == ["edit", "draft"]
    for j in (review, edit):
        store.set_job_state(j.id, "done", "", T1)
        store.save_run(JobRun(j.id, request={"job": review.id, "number": 2, "instruction": "shorter"}))
    assert [j.id for j, _ in store.done_unsent()] == [review.id]
    store.set_job_state(review.id, "posted", "", T1)
    assert [j.id for j, _ in store.done_unsent()] == [review.id]   # posted from part 1: the rest still goes out
    assert store.run(edit.id).request["instruction"] == "shorter"
    store.set_job_state(edit.id, "running", "", T1)
    assert store.jobs_in_state("running", kinds=("review", "recheck")) == []
