# tests/unit/test_skillflow_checkout.py
from __future__ import annotations
import asyncio, base64, shutil, subprocess
from pathlib import Path
import pytest
from integrations.base.ticket_key import carries_key
from orchestrator.reviewer.commands import CommandResult, run_command
from orchestrator.skillflow.checkout import CheckoutError, SkillCheckout

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

def commit(work, message):
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", message, cwd=work)

def commit_and_push(work, name):
    (work / name).write_text(name)
    git("add", ".", cwd=work)
    commit(work, name)
    git("push", "origin", "HEAD:develop", cwd=work)
    return git("rev-parse", "HEAD", cwd=work)

def commit_and_push_all(work):
    git("add", ".", cwd=work)
    commit(work, "all")
    git("push", "origin", "HEAD:develop", cwd=work)

def commit_and_push_branch(work, name, branch, force=False):
    (work / name).write_text(name)
    git("add", ".", cwd=work)
    commit(work, name)
    git("push", *(["--force"] if force else []), "origin", f"HEAD:refs/heads/{branch}", cwd=work)

def make_origin(tmp_path):
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    git("init", "--bare", "-b", "develop", str(bare))
    git("clone", str(bare), str(work))
    (work / "docs").mkdir()
    (work / "docs" / "process.md").write_text("# Process\n")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "a", cwd=work)
    git("add", ".", cwd=work)
    commit(work, "b")
    git("push", "origin", "HEAD:develop", cwd=work)
    return bare, work

@pytest.fixture
def repo(tmp_path):
    """origin.git with develop, and `checkout`: a hand-made clone of it."""
    bare, work = make_origin(tmp_path)
    git("clone", str(bare), str(tmp_path / "checkout"))
    return bare, work, SkillCheckout(tmp_path / "checkout", f"file://{bare}", "develop", "tok")

def add_worktree(co, key="ACME-1", branch="ACME-1-fix"):
    wt = co.worktree(co.path, f".claude/worktrees/{key}")
    git("worktree", "add", "--no-track", "-b", branch, str(wt), "origin/develop", cwd=co.path)
    return wt

def tip(co, branch="ACME-1-fix"):
    """The branch's commit in the checkout: the commit the runner hands push, the one its gates passed."""
    return git("rev-parse", f"refs/heads/{branch}", cwd=co.path)

async def test_prepare_fetches_and_leaves_the_skill_checkout_where_it_is(repo):
    bare, work, co = repo
    before = git("rev-parse", "HEAD", cwd=co.path)
    sha = commit_and_push(work, "new.txt")
    assert await co.prepare() == sha == git("rev-parse", "refs/remotes/origin/develop", cwd=co.path)
    assert git("rev-parse", "HEAD", cwd=co.path) == before and not (co.path / "new.txt").exists()

async def test_a_run_checkout_is_a_worktree_of_its_own_at_the_base(repo, tmp_path):
    bare, work, co = repo
    (co.path / "local.properties").write_text("sdk.dir=/sdk")       # the operator's, not the run's
    sha = commit_and_push(work, "new.txt")
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run)
    assert git("rev-parse", "HEAD", cwd=run) == sha and git("branch", "--show-current", cwd=run) == ""
    assert (run / "new.txt").exists() and not (run / "local.properties").exists()
    assert not (co.path / "new.txt").exists()                       # the skill checkout itself does not move
    assert git("rev-parse", "--git-common-dir", cwd=run) == str((co.path / ".git").resolve())

async def test_a_run_checkout_made_again_moves_to_the_new_base_and_keeps_untracked_files(repo, tmp_path):
    bare, work, co = repo
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run)
    (run / "local.properties").write_text("sdk.dir=/sdk")
    sha = commit_and_push(work, "new.txt")
    await co.prepare()
    await co.make_run_checkout(run)                                 # a prepare Approve runs again
    assert git("rev-parse", "HEAD", cwd=run) == sha and (run / "local.properties").exists()

async def test_a_run_checkout_refuses_to_overwrite_local_changes(repo, tmp_path):
    bare, work, co = repo
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run)
    (run / "docs" / "process.md").write_text("mine")
    (work / "docs" / "process.md").write_text("theirs"); commit_and_push_all(work)
    await co.prepare()
    with pytest.raises(CheckoutError) as e:
        await co.make_run_checkout(run)
    reason = str(e.value)
    assert reason.startswith("the run's checkout could not move to origin/develop: ")
    assert "local changes" in reason and "docs/process.md" in reason
    assert (run / "docs" / "process.md").read_text() == "mine"

async def test_a_run_checkout_never_takes_a_path_that_is_not_its_own(repo, tmp_path):
    _, _, co = repo
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    run.mkdir(parents=True); (run / "keep.txt").write_text("keep")
    with pytest.raises(CheckoutError, match="the run's checkout could not be made"):
        await co.make_run_checkout(run)
    assert sorted(p.name for p in run.iterdir()) == ["keep.txt"]

async def test_copy_files_brings_the_required_files_from_the_skill_checkout(repo, tmp_path):
    _, _, co = repo
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run)
    (co.path / "local.properties").write_text("sdk.dir=/sdk")
    (co.path / "app" / "keys").mkdir(parents=True); (co.path / "app" / "keys" / "debug.json").write_text("{}")
    co.copy_files(run, ["local.properties", "app/keys/debug.json"])
    assert (run / "local.properties").read_text() == "sdk.dir=/sdk"
    assert (run / "app" / "keys" / "debug.json").read_text() == "{}"
    (co.path / "local.properties").write_text("sdk.dir=/new")
    co.copy_files(run, ["local.properties"])                        # an earlier copy is replaced
    assert (run / "local.properties").read_text() == "sdk.dir=/new"

async def test_copy_files_never_writes_through_a_symlink_nor_copies_a_directory(repo, tmp_path):
    _, _, co = repo
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run)
    (co.path / "local.properties").write_text("sdk.dir=/sdk")
    outside = tmp_path / "outside.txt"; outside.write_text("keep")
    (run / "local.properties").symlink_to(outside)
    with pytest.raises(CheckoutError, match="symlink"):
        co.copy_files(run, ["local.properties"])
    assert outside.read_text() == "keep"
    (co.path / "keys").mkdir()
    with pytest.raises(CheckoutError, match="not a file"):
        co.copy_files(run, ["keys"])

async def test_the_skills_worktree_in_a_run_checkout_is_checked_pushed_and_removed(repo, tmp_path):
    # The skill makes its worktree inside the run's checkout, with git worktree add there: it is the skill
    # checkout's worktree, like the run's checkout itself.
    bare, _, co = repo
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run)
    wt = co.worktree(run, ".claude/worktrees/ACME-1")
    git("worktree", "add", "--no-track", "-b", "ACME-1-fix", str(wt), "origin/develop", cwd=run)
    (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    assert await co.has_change(wt) is True and await co.commit_state(wt) == ("ACME-1-fix", "")
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)
    assert await co.remove_worktree(wt) == "" and not wt.exists()
    assert await co.remove_worktree(run) == "" and not run.exists()
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=co.path) == git("rev-parse", "refs/heads/ACME-1-fix",
                                                                         cwd=bare)   # the branch stays

async def test_a_run_checkout_holding_an_untracked_file_is_never_removed(repo, tmp_path):
    _, _, co = repo
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run)
    (run / "REPORT.md").write_text("the session's")
    assert "untracked" in await co.remove_worktree(run) and (run / "REPORT.md").exists()

async def test_runs_that_prepare_and_push_at_once_take_turns_in_the_skill_checkout(repo, tmp_path):
    # The fetch, the courier and the worktree list are the skill checkout's, shared by every run of the repo.
    bare, work, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    commit_and_push(work, "new.txt")
    runs = [tmp_path / "runs" / f"r{i}" / "checkout" for i in range(3)]

    async def prepare(run):
        await co.prepare()
        await co.make_run_checkout(run)

    await asyncio.gather(*(prepare(run) for run in runs), co.push("ACME-1-fix", tip(co)))
    assert all((run / "new.txt").exists() for run in runs)
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == tip(co)

async def test_prepare_fetches_other_branches(repo):
    bare, work, co = repo
    git("push", "origin", "HEAD:refs/heads/ACME-1-old-try", cwd=work)
    await co.prepare()
    assert await co.branches_with("acme-1") == ["ACME-1-old-try"] and await co.branches_with("ACME-9") == []

async def test_branches_with_holds_the_key_as_a_whole_key(repo):
    # As open_prs_for_key reads it: ACME-1 is not ACME-10.
    bare, work, co = repo
    git("push", "origin", "HEAD:refs/heads/feature/ACME-10-other", cwd=work)
    await co.prepare()
    assert await co.branches_with("ACME-1") == []
    assert await co.branches_with("ACME-10") == ["feature/ACME-10-other"]

@pytest.mark.parametrize("text, carries", [
    ("CAB-12-login", False), ("WEBAB-12: x", False), ("AB-123", False),
    ("feature/AB-12", True), ("ab-12-fix", True), ("(AB-12)", True), ("AB-12", True),
])
def test_a_whole_key_is_not_part_of_a_longer_word(text, carries):
    assert carries_key(text, "AB-12") is carries

async def test_branches_with_leaves_out_a_key_inside_a_longer_one(repo):
    bare, work, co = repo
    for branch in ("CAB-12-login", "feature/AB-12"):
        git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=work)
    await co.prepare()
    assert await co.branches_with("AB-12") == ["feature/AB-12"]

async def test_on_forge_is_a_fetched_branch_of_that_whole_name(repo):
    bare, work, co = repo
    git("push", "origin", "HEAD:refs/heads/release/2.0", cwd=work)
    await co.prepare()
    git("branch", "ACME-1-fix", cwd=co.path)                        # the session's own, not on the forge
    assert await co.on_forge("release/2.0") is True and await co.on_forge("develop") is True
    assert await co.on_forge("release") is False and await co.on_forge("ACME-1-fix") is False

async def test_prepare_without_a_checkout_says_where(tmp_path):
    with pytest.raises(CheckoutError, match="clone it by hand"):
        await SkillCheckout(tmp_path / "none", "file:///x", "develop", "tok").prepare()

def test_missing_files_and_ticket_copy(repo, tmp_path):
    _, _, co = repo
    assert co.missing_files(["local.properties", "docs/process.md"]) == ["local.properties"]
    src = tmp_path / "ticket"; (src / "attachments").mkdir(parents=True)
    (src / "ticket.md").write_text("# ACME-1"); (src / "attachments" / "a.png").write_bytes(b"x")
    dest = co.write_ticket(co.path, src, "ai_instructions/ACME-1")
    assert dest == co.path / "ai_instructions/ACME-1/ticket" and (dest / "attachments/a.png").exists()

def test_write_ticket_replaces_only_an_earlier_copy(repo, tmp_path):
    _, _, co = repo
    src = tmp_path / "ticket"; src.mkdir()
    (src / "old.png").write_bytes(b"x")
    dest = co.write_ticket(co.path, src, "ai_instructions/ACME-1")
    (dest.parent / "notes.md").write_text("the session's own")
    (src / "old.png").unlink(); (src / "ticket.md").write_text("# ACME-1")
    assert co.write_ticket(co.path, src, "ai_instructions/ACME-1") == dest
    assert not (dest / "old.png").exists() and (dest / "ticket.md").read_text() == "# ACME-1"
    assert (dest.parent / "notes.md").read_text() == "the session's own"

def test_write_ticket_refuses_a_symlink_or_a_file_at_ticket(repo, tmp_path):
    _, _, co = repo
    src = tmp_path / "ticket"; src.mkdir(); (src / "ticket.md").write_text("# ACME-1")
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "keep.txt").write_text("keep")
    notes = co.path / "ai_instructions" / "ACME-1"; notes.mkdir(parents=True)
    (notes / "ticket").symlink_to(outside)
    with pytest.raises(CheckoutError, match="symlink"):
        co.write_ticket(co.path, src, "ai_instructions/ACME-1")
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]     # never followed
    (notes / "ticket").unlink(); (notes / "ticket").write_text("a file")
    with pytest.raises(CheckoutError, match="ticket"):
        co.write_ticket(co.path, src, "ai_instructions/ACME-1")

def test_write_ticket_in_an_unreadable_notes_directory_is_a_checkout_error(repo, tmp_path):
    _, _, co = repo
    src = tmp_path / "ticket"; src.mkdir(); (src / "ticket.md").write_text("# ACME-1")
    notes = co.path / "ai_instructions" / "ACME-1"; notes.mkdir(parents=True)
    notes.chmod(0o000)
    try:
        with pytest.raises(CheckoutError, match="ticket"):
            co.write_ticket(co.path, src, "ai_instructions/ACME-1")
    finally:
        notes.chmod(0o755)

def test_worktree_must_stay_inside(repo):
    with pytest.raises(CheckoutError):
        repo[2].worktree(repo[2].path, "../outside")

def test_unusable_paths_are_checkout_errors(repo, tmp_path):
    _, _, co = repo
    with pytest.raises(CheckoutError):
        co.worktree(co.path, str(tmp_path / "elsewhere"))                          # absolute
    (co.path / "loop").symlink_to(co.path / "loop")
    with pytest.raises(CheckoutError, match="loop"):
        co.worktree(co.path, "loop/ACME-1")
    src = tmp_path / "ticket"; src.mkdir()
    with pytest.raises(CheckoutError, match="loop"):
        co.write_ticket(co.path, src, "loop/ACME-1")

async def test_change_and_commit_state(repo):
    _, _, co = repo
    await co.prepare()
    assert await co.has_change(co.worktree(co.path, ".claude/worktrees/ACME-1")) is False      # no worktree
    wt = add_worktree(co)
    assert await co.has_change(wt) is False
    (wt / "fix.txt").write_text("fix")
    assert await co.has_change(wt) is True
    assert (await co.commit_state(wt))[1].startswith("not committed")                # untracked file
    git("add", ".", cwd=wt); commit(wt, "fix")
    assert await co.commit_state(wt) == ("ACME-1-fix", "") and await co.changed_files(wt) == ["fix.txt"]
    assert await co.has_change(wt) is True

async def test_commit_state_without_a_commit_or_on_a_detached_head(repo):
    _, _, co = repo
    await co.prepare()
    wt = add_worktree(co)
    assert "no commit" in (await co.commit_state(wt))[1]
    git("checkout", "--detach", cwd=wt)
    assert "not on a branch" in (await co.commit_state(wt))[1]

async def test_push_sends_the_branch_and_never_forces(repo, tmp_path):
    bare, work, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)
    assert "tok" not in (co.path / ".git" / "config").read_text()
    git("checkout", "-b", "ACME-1-fix", cwd=work); commit_and_push_branch(work, "other.txt", "ACME-1-fix", force=True)
    (wt / "more.txt").write_text("m"); git("add", ".", cwd=wt); commit(wt, "more")
    with pytest.raises(CheckoutError, match="push"):
        await co.push("ACME-1-fix", tip(co))

async def test_rejected_push_says_why(repo):
    bare, work, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    git("checkout", "-b", "ACME-1-fix", cwd=work); commit_and_push_branch(work, "other.txt", "ACME-1-fix")
    with pytest.raises(CheckoutError) as e:
        await co.push("ACME-1-fix", tip(co))
    reason = str(e.value)
    assert reason.startswith("push rejected: ") and "[rejected]" in reason
    assert ("non-fast-forward" in reason or "fetch first" in reason) and "hint" not in reason

async def test_a_second_prepare_and_push_of_a_moved_branch(repo):
    bare, _, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    await co.push("ACME-1-fix", tip(co))
    (wt / "more.txt").write_text("m"); git("add", ".", cwd=wt); commit(wt, "more")
    await co.prepare()
    await co.push("ACME-1-fix", tip(co))
    head = git("rev-parse", "HEAD", cwd=wt)
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == head
    # A rewritten branch is staged all the same; the forge, without force, refuses it and keeps what it had.
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--amend", "-m", "again", cwd=wt)
    with pytest.raises(CheckoutError, match=r"\[rejected\] +refs/skill/ACME-1-fix -> ACME-1-fix"):
        await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == head

def staged_refs(tmp_path):
    return git("--git-dir", str(tmp_path / "checkout.courier.git"), "for-each-ref", "refs/skill/")

async def test_the_staging_ref_never_outlives_its_push(repo, tmp_path):
    # The checkout may prune what a leftover staging ref points at, and a ref to missing objects can break the
    # courier's next fetch.
    bare, work, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)
    assert staged_refs(tmp_path) == ""
    git("checkout", "-b", "ACME-1-fix", cwd=work)
    commit_and_push_branch(work, "other.txt", "ACME-1-fix", force=True)
    (wt / "more.txt").write_text("m"); git("add", ".", cwd=wt); commit(wt, "more")
    with pytest.raises(CheckoutError, match=r"\[rejected\]"):
        await co.push("ACME-1-fix", tip(co))
    assert staged_refs(tmp_path) == ""

async def test_a_failed_staging_cleanup_leaves_the_push_result(repo, monkeypatch, tmp_path):
    bare, work, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    forge_pushed = False
    async def cleanup_fails(argv, **kwargs):     # every update-ref after the forge push fails
        nonlocal forge_pushed
        if forge_pushed and "update-ref" in argv:
            return CommandResult(returncode=1, stdout="", stderr="fatal: cannot lock ref\n", timed_out=False)
        forge_pushed = forge_pushed or f"file://{bare}" in argv
        return await run_command(argv, **kwargs)
    monkeypatch.setattr("orchestrator.skillflow.checkout.run_command", cleanup_fails)
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)
    # The leftover is replaced before the next staging push, and the forge's refusal is what is reported.
    forge_pushed = False
    git("checkout", "-b", "ACME-1-fix", cwd=work)
    commit_and_push_branch(work, "other.txt", "ACME-1-fix", force=True)
    (wt / "more.txt").write_text("m"); git("add", ".", cwd=wt); commit(wt, "more")
    with pytest.raises(CheckoutError, match=r"\[rejected\]") as e:
        await co.push("ACME-1-fix", tip(co))
    assert "cannot lock" not in str(e.value)

async def test_the_courier_borrows_the_checkouts_objects(repo, tmp_path):
    _, work, co = repo
    base = git("rev-parse", "HEAD", cwd=co.path)
    commit_and_push(work, "new.txt")
    await co.prepare()
    courier = tmp_path / "checkout.courier.git"
    assert courier.is_dir() and not courier.resolve().is_relative_to(co.path.resolve())
    alternates = (courier / "objects" / "info" / "alternates").read_text()
    assert alternates.splitlines() == [str(co.path / ".git" / "objects")]
    counts = dict(line.split(": ", 1)
                  for line in git("--git-dir", str(courier), "count-objects", "-v").splitlines())
    assert counts["packs"] == "0" and not (courier / "objects" / base[:2] / base[2:]).exists()
    git("--git-dir", str(courier), "cat-file", "-e", base)                  # there through the alternate

async def test_prepare_heals_a_branch_that_became_a_directory(repo):
    # The session's own `fetch --prune` in the checkout turned origin/feature into origin/feature/x; the courier
    # of the run before still holds origin/feature.
    bare, work, co = repo
    git("push", "origin", "HEAD:refs/heads/feature", cwd=work)
    await co.prepare()
    git("update-ref", "-d", "refs/heads/feature", cwd=bare)
    git("push", "origin", "HEAD:refs/heads/feature/x", cwd=work)
    git("fetch", "--prune", cwd=co.path)
    await co.prepare()
    assert await co.branches_with("feature") == ["feature/x"]

async def test_prepare_heals_an_interrupted_one(repo, tmp_path):
    # Stopped after the forge fetch: the courier holds origin/feature/x, the checkout still origin/feature.
    bare, work, co = repo
    git("push", "origin", "HEAD:refs/heads/feature", cwd=work)
    await co.prepare()
    git("update-ref", "-d", "refs/heads/feature", cwd=bare)
    git("push", "origin", "HEAD:refs/heads/feature/x", cwd=work)
    git("--git-dir", str(tmp_path / "checkout.courier.git"),
        "fetch", "--prune", f"file://{bare}", "+refs/heads/*:refs/remotes/origin/*")
    await co.prepare()
    assert await co.branches_with("feature") == ["feature/x"]

async def test_prepare_after_the_checkout_was_cloned_again(repo):
    # The courier next to the checkout outlives the old clone; its origin/old names a commit it only borrowed
    # from that clone (the session had fetched it), which the new one lacks.
    bare, work, co = repo
    git("checkout", "-b", "old", cwd=work); commit_and_push_branch(work, "old.txt", "old")
    git("fetch", cwd=co.path)
    await co.prepare()
    git("update-ref", "-d", "refs/heads/old", cwd=bare)
    shutil.rmtree(co.path); git("clone", f"file://{bare}", str(co.path))
    assert await co.prepare() == git("rev-parse", "develop", cwd=bare)
    assert await co.branches_with("old") == []

async def test_prepare_after_a_local_tag_lost_its_commit(repo):
    # The checkout's own tag on its own commit, then both gone (`tag -d`, then gc): a courier that copied the
    # tag names a missing object.
    bare, _, co = repo
    sha = git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "HEAD^{tree}", "-m", "local",
              cwd=co.path)
    git("tag", "local-only", sha, cwd=co.path)
    await co.prepare()
    git("tag", "-d", "local-only", cwd=co.path)
    (co.path / ".git" / "objects" / sha[:2] / sha[2:]).unlink()          # what gc does to it
    assert await co.prepare() == git("rev-parse", "develop", cwd=bare)

async def test_tags_reach_the_checkout_from_the_forge_only(repo):
    _, work, co = repo
    git("tag", "local-only", cwd=co.path)
    commit_and_push(work, "new.txt"); git("tag", "v1", cwd=work); git("push", "origin", "v1", cwd=work)
    await co.prepare()
    git("tag", "-d", "local-only", cwd=co.path)
    await co.prepare()
    assert git("tag", "-l", cwd=co.path).splitlines() == ["v1"]

async def test_a_directory_cleave_did_not_make_is_never_removed(repo, tmp_path):
    _, _, co = repo
    foreign = tmp_path / "checkout.courier.git"; foreign.mkdir(); (foreign / "keep.txt").write_text("keep")
    with pytest.raises(CheckoutError, match="remove it by hand") as e:
        await co.prepare()
    assert str(foreign) in str(e.value)
    git("branch", "ACME-1-fix", cwd=co.path)
    with pytest.raises(CheckoutError, match="remove it by hand"):
        await co.push("ACME-1-fix", tip(co))
    assert sorted(p.name for p in foreign.iterdir()) == ["keep.txt"]

async def test_push_makes_a_courier_when_there_is_none(repo, tmp_path):
    bare, _, co = repo
    courier = tmp_path / "checkout.courier.git"
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    shutil.rmtree(courier)
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)

async def test_push_heals_a_ref_that_dangles_in_the_courier(repo, tmp_path):
    # A ref seeded at prepare time can dangle by push time: the branch deleted on the forge, then a prune and a
    # gc in the checkout.
    bare, _, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    dangling = tmp_path / "checkout.courier.git" / "refs" / "remotes" / "origin" / "gone"
    dangling.write_text("dead" * 10 + "\n")                                  # names no object
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)

async def test_a_courier_removal_stopped_part_way_is_still_cleaves(repo, tmp_path):
    # The mark goes last: a removal that fails part-way leaves a courier the next prepare may still remove.
    bare, _, co = repo
    await co.prepare()
    courier = tmp_path / "checkout.courier.git"
    stuck = courier / "refs" / "remotes" / "origin"                         # holds develop
    stuck.chmod(0o500)
    try:
        with pytest.raises(CheckoutError):
            await co.prepare()
        assert (courier / "cleave-courier").is_file()
    finally:
        stuck.chmod(0o755)
    assert await co.prepare() == git("rev-parse", "develop", cwd=bare)

async def test_an_empty_directory_at_the_courier_path_is_cleaves(repo, tmp_path):
    # A stop between making the courier's directory and writing its mark leaves it empty.
    bare, _, co = repo
    (tmp_path / "checkout.courier.git").mkdir()
    assert await co.prepare() == git("rev-parse", "develop", cwd=bare)

def test_the_courier_goes_only_next_to_the_checkout(tmp_path):
    # A courier path given by hand could name the checkout itself, which making the courier anew removes.
    with pytest.raises(TypeError):
        SkillCheckout(tmp_path / "checkout", "file:///x", "develop", "tok", courier=tmp_path / "checkout")

async def test_push_heals_an_alternates_file_that_is_not_utf8(repo, tmp_path):
    bare, _, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    alternates = tmp_path / "checkout.courier.git" / "objects" / "info" / "alternates"
    alternates.write_bytes(b"\xff\xfe not utf-8\n")
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)
    assert alternates.read_text().splitlines() == [str(co.path / ".git" / "objects")]

async def test_a_symlink_at_the_courier_path_is_never_followed(repo, tmp_path):
    # Even to a directory that holds the mark: what it points at is not Cleave's courier.
    _, _, co = repo
    await co.prepare()
    courier, target = tmp_path / "checkout.courier.git", tmp_path / "elsewhere.git"
    courier.rename(target); courier.symlink_to(target)
    files = sorted(p.relative_to(target) for p in target.rglob("*"))
    assert Path("cleave-courier") in files
    with pytest.raises(CheckoutError, match="remove it by hand"):
        await co.prepare()
    git("branch", "ACME-1-fix", cwd=co.path)
    with pytest.raises(CheckoutError, match="remove it by hand"):
        await co.push("ACME-1-fix", tip(co))
    assert sorted(p.relative_to(target) for p in target.rglob("*")) == files

async def test_prepare_refuses_a_shallow_checkout(repo, tmp_path):
    bare, _, _ = repo
    shallow = tmp_path / "shallow"
    git("clone", "--depth", "1", f"file://{bare}", str(shallow))
    with pytest.raises(CheckoutError,
                       match=r"^the skill checkout is a shallow clone: clone it in full \(deploy/README\.md\)$"):
        await SkillCheckout(shallow, f"file://{bare}", "develop", "tok").prepare()

async def test_the_calls_between_checkout_and_courier_leave_submodules_alone(repo, monkeypatch):
    # A submodule's own fetch or push would run without the token, and read that submodule's config.
    _, _, co = repo
    calls = []
    async def recording(argv, **kwargs):
        calls.append(argv)
        return await run_command(argv, **kwargs)
    monkeypatch.setattr("orchestrator.skillflow.checkout.run_command", recording)
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    await co.push("ACME-1-fix", tip(co))
    fetches = [argv for argv in calls if argv[1] == "-C" and argv[3] == "fetch"]
    pushes = [argv for argv in calls if argv[1] == "-C" and argv[3] == "push"]
    assert len(fetches) == 1 and "--no-recurse-submodules" in fetches[0]
    assert len(pushes) == 1 and "--recurse-submodules=no" in pushes[0]

async def test_git_speaks_english_whatever_the_locale(repo, monkeypatch):
    # _reason looks for git's own words, and the reason reaches the operator in English.
    monkeypatch.setenv("LANG", "uk_UA.UTF-8")
    _, work, co = repo
    await co.prepare()
    run = co.path.parent / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run)
    (run / "docs" / "process.md").write_text("mine")
    (work / "docs" / "process.md").write_text("theirs"); commit_and_push_all(work)
    await co.prepare()
    with pytest.raises(CheckoutError) as e:
        await co.make_run_checkout(run)
    assert "local changes" in str(e.value) and "docs/process.md" in str(e.value)
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    git("checkout", "-b", "ACME-1-fix", cwd=work); commit_and_push_branch(work, "other.txt", "ACME-1-fix")
    with pytest.raises(CheckoutError) as e:
        await co.push("ACME-1-fix", tip(co))
    assert "[rejected]" in str(e.value) and ("fetch first" in str(e.value) or "non-fast-forward" in str(e.value))

async def test_remove_worktree_never_forces(repo):
    _, _, co = repo
    await co.prepare()
    wt = add_worktree(co)
    (wt / "dirty.txt").write_text("x")
    assert await co.remove_worktree(wt) != "" and wt.exists()
    (wt / "dirty.txt").unlink()
    assert await co.remove_worktree(wt) == "" and not wt.exists()
    assert await co.remove_worktree(wt) == ""

async def test_a_run_checkout_removed_by_hand_reads_as_removed_and_is_made_again(repo, tmp_path):
    # Its directory gone, git still lists it: it would neither move nor remove it.
    _, _, co = repo
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run)
    shutil.rmtree(run)
    assert await co.remove_worktree(run) == ""
    await co.make_run_checkout(run)
    shutil.rmtree(run)
    await co.make_run_checkout(run)
    assert git("branch", "--show-current", cwd=run) == "" and (run / ".git").is_file()

async def test_remove_worktree_tells_a_path_it_cannot_resolve(repo):
    _, _, co = repo
    (co.path / "loop").symlink_to(co.path / "loop")
    assert "cannot be resolved" in await co.remove_worktree(co.path / "loop" / "ACME-1")

async def test_git_errors_are_redacted_and_timeouts_named(repo, monkeypatch):
    _, _, co = repo
    git("branch", "ACME-1-fix", cwd=co.path)
    co = SkillCheckout(co.path, "file:///nowhere", "develop", token="SECRET", timeout=5)
    results = [CommandResult(returncode=128, stdout="", stderr="fatal: SECRET denied\n", timed_out=False),
               CommandResult(returncode=-9, stdout="", stderr="", timed_out=True, timeout=5)]
    async def fake(argv, **kwargs):     # the forge's answers; the calls between checkout and courier run
        return results.pop(0) if "file:///nowhere" in argv else await run_command(argv, **kwargs)
    monkeypatch.setattr("orchestrator.skillflow.checkout.run_command", fake)
    with pytest.raises(CheckoutError) as e:
        await co.push("ACME-1-fix", tip(co))
    assert str(e.value) == "push rejected: git push failed: fatal: *** denied"
    with pytest.raises(CheckoutError, match=r"^git fetch timed out after 5 s$"):
        await co.prepare()
    assert co.redact("x SECRET y") == "x *** y"

@pytest.mark.parametrize("stderr, reason", [
    ("To x\n ! [remote rejected] ACME-1-fix -> ACME-1-fix (protected branch hook declined)\n"
     "error: failed to push some refs to 'x'\nhint: see the docs\n",
     "! [remote rejected] ACME-1-fix -> ACME-1-fix (protected branch hook declined)"),
    ("error: overwritten by checkout:\n\ta.kt\n\tb.kt\n\tc.kt\n\td.kt\nPlease commit them.\nAborting\n",
     "error: overwritten by checkout: a.kt, b.kt, c.kt"),
    ("warning: one\nfatal: SECRET denied\nhint: two\n", "fatal: *** denied"),
    ("hint: one\nhint: two\n\n", "hint: two"),
    ("fatal: " + "x" * 400 + "\n", "fatal: " + "x" * 293),
    ("fatal: " + "SECRET" * 60 + "\n", "fatal: " + "***" * 60),     # redacted before the cut: no part leaks
])
async def test_git_error_reason_is_the_line_that_says_why(repo, monkeypatch, stderr, reason):
    _, _, co = repo
    git("branch", "ACME-1-fix", cwd=co.path)
    co = SkillCheckout(co.path, "file:///nowhere", "develop", token="SECRET")
    async def fake(argv, **kwargs):     # the forge's answer; the calls between checkout and courier run
        if "file:///nowhere" in argv:
            return CommandResult(returncode=1, stdout="", stderr=stderr, timed_out=False)
        return await run_command(argv, **kwargs)
    monkeypatch.setattr("orchestrator.skillflow.checkout.run_command", fake)
    with pytest.raises(CheckoutError) as e:
        await co.push("ACME-1-fix", tip(co))
    assert str(e.value) == f"push rejected: git push failed: {reason}"

async def test_only_the_fetch_and_the_push_carry_the_token(repo, monkeypatch):
    # The session can leave a program-running setting in the checkout's .git/config (a filter driver, say): a
    # local call that runs it must not hand it the token.
    _, _, co = repo
    header = base64.b64encode(b"x-access-token:tok").decode()
    calls = []
    async def recording(argv, **kwargs):
        calls.append((argv[3:], kwargs["env"]))
        return await run_command(argv, **kwargs)
    monkeypatch.setattr("orchestrator.skillflow.checkout.run_command", recording)
    await co.prepare()
    run = co.path.parent / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run); await co.make_run_checkout(run)      # made, then moved
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    await co.branches_with("ACME-1"); await co.has_change(wt); await co.on_forge("ACME-1-fix")
    await co.commit_state(wt); await co.changed_files(wt)
    await co.push("ACME-1-fix", tip(co))
    assert await co.remove_worktree(wt) == "" and await co.remove_worktree(run) == ""
    assert [args[0] for args, env in calls if any(header in v for v in env.values())] == ["fetch", "push"]
    local = {word for args, env in calls if not any(header in v for v in env.values()) for word in args}
    assert {"checkout", "status", "rev-list", "branch", "for-each-ref", "worktree", "diff"} <= local
    for args, env in calls:
        config = config_entries(env)
        assert config["core.hooksPath"] == "/dev/null" and config["core.fsmonitor"] == "false"
        assert config["gc.autoDetach"] == "false" and config["maintenance.autoDetach"] == "false"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_TERMINAL_PROMPT"] == "0"
        if any(header in v for v in env.values()):
            assert env["GIT_ALLOW_PROTOCOL"] == "file"
            assert {key: config.get(key) for key in NETWORK_OVERRIDES} == NETWORK_OVERRIDES

async def test_the_token_goes_only_to_the_courier(repo, monkeypatch, tmp_path):
    # The courier's config is Cleave's; the checkout's and its worktrees' are the session's to write.
    _, _, co = repo
    header = base64.b64encode(b"x-access-token:tok").decode()
    calls = []
    async def recording(argv, **kwargs):
        calls.append((argv, any(header in v for v in kwargs["env"].values())))
        return await run_command(argv, **kwargs)
    monkeypatch.setattr("orchestrator.skillflow.checkout.run_command", recording)
    await co.prepare()
    run = tmp_path / "runs" / "r1" / "checkout"
    await co.make_run_checkout(run); await co.make_run_checkout(run)      # made, then moved
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    await co.push("ACME-1-fix", tip(co))
    in_courier = ["--git-dir", str(tmp_path / "checkout.courier.git")]
    with_token = [argv for argv, token in calls if token]
    assert [argv[3] for argv in with_token] == ["fetch", "push"]
    assert all(argv[1:3] == in_courier for argv in with_token)
    in_checkout = [(argv, token) for argv, token in calls if argv[1] == "-C"]
    assert {word for argv, _ in in_checkout for word in argv[3:]} >= {"fetch", "worktree", "checkout", "push"}
    assert all(Path(argv[2]).is_relative_to(co.path) or Path(argv[2]) == run for argv, _ in in_checkout)
    assert not any(token for _, token in in_checkout)
    assert len(in_checkout) + sum(argv[1:3] == in_courier for argv, _ in calls) == len(calls)

NETWORK_OVERRIDES = {"core.alternateRefsCommand": "true", "push.gpgSign": "false",
                     "fetch.recurseSubmodules": "false", "push.recurseSubmodules": "no", "submodule.recurse": "false"}

def config_entries(env):
    """The git config an environment sets through GIT_CONFIG_COUNT / KEY_n / VALUE_n."""
    count = int(env["GIT_CONFIG_COUNT"])
    return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}

async def test_fetch_runs_no_planted_alternate_refs_command(repo, tmp_path):
    # Fetch negotiation runs core.alternateRefsCommand through the shell when the checkout has alternates; the
    # session can write both. The checkout's own token-less fetch may run it, the fetch from the forge must not.
    bare, work, co = repo
    marker, script = tmp_path / "alternate-refs-saw-token", tmp_path / "alternate-refs.sh"
    script.write_text(f"#!/bin/sh\nenv | grep -q AUTHORIZATION && touch '{marker}'\nexit 0\n"); script.chmod(0o755)
    (co.path / ".git" / "objects" / "info" / "alternates").write_text(f"{work / '.git' / 'objects'}\n")
    with (co.path / ".git" / "config").open("a") as f:
        f.write(f"[core]\n\talternateRefsCommand = {script}\n")
    sha = commit_and_push(work, "new.txt")
    assert await co.prepare() == sha and not marker.exists()

async def test_push_runs_no_planted_signing_program(repo, tmp_path):
    # push.gpgSign makes the push run gpg.program, both read from the checkout's .git/config, with the token in
    # the environment; a receiving end that takes signed pushes (a nonce seed) lets it start.
    bare, work, co = repo
    git("config", "receive.certNonceSeed", "seed", cwd=bare)
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    marker, script = tmp_path / "gpg-ran", tmp_path / "gpg.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n"); script.chmod(0o755)
    with (co.path / ".git" / "config").open("a") as f:
        f.write(f"[push]\n\tgpgSign = true\n[gpg]\n\tprogram = {script}\n[user]\n\tname = t\n\temail = t@t\n")
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)
    assert not marker.exists()

def plant_token_spy_hooks(co, tmp_path):
    """Config-defined hooks in the checkout's .git/config for reference-transaction (a fetch) and pre-push (a
    push); each appends its event to the returned file only when its environment holds the token's header."""
    marker, script = tmp_path / "hook-saw-token", tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\ncat >/dev/null\n"
                      f"env | grep -q AUTHORIZATION && echo \"$1\" >> '{marker}'\nexit 0\n")
    script.chmod(0o755)
    with (co.path / ".git" / "config").open("a") as f:
        for event in ("reference-transaction", "pre-push"):
            f.write(f'[hook "spy-{event}"]\n\tcommand = {script} {event}\n\tevent = {event}\n')
    return marker

async def test_config_defined_hooks_never_see_the_token(repo, tmp_path):
    # hook.<name>.command runs whatever core.hooksPath says: reference-transaction on a fetch, pre-push on a
    # push. The session can write both into the checkout's .git/config.
    bare, work, co = repo
    marker = plant_token_spy_hooks(co, tmp_path)
    sha = commit_and_push(work, "new.txt")
    assert await co.prepare() == sha
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)
    assert not marker.exists() or marker.read_text() == ""

async def test_fetch_and_push_ignore_a_planted_transport(repo, tmp_path):
    # An insteadOf the session left in .git/config would send the call through ssh and run its program with the
    # token in the environment; the calls to the forge read the courier's config, not that one.
    bare, work, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    marker, script = tmp_path / "ssh-ran", tmp_path / "ssh.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n"); script.chmod(0o755)
    with (co.path / ".git" / "config").open("a") as f:
        f.write(f'[url "ssh://git@acme.invalid/x.git"]\n\tinsteadOf = file://{bare}\n'
                f"[core]\n\tsshCommand = {script}\n")
    await co.push("ACME-1-fix", tip(co))
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == git("rev-parse", "HEAD", cwd=wt)
    assert not marker.exists()
    assert await co.prepare() == git("rev-parse", "develop", cwd=bare)
    assert not marker.exists()

# --- Task 12, review round 1 ---

async def test_push_refuses_the_base_branch_before_any_git(repo, monkeypatch):
    bare, _, co = repo
    before = git("rev-parse", "refs/heads/develop", cwd=bare)
    calls = []
    async def recording(argv, **kwargs):
        calls.append(argv)
        return await run_command(argv, **kwargs)
    monkeypatch.setattr("orchestrator.skillflow.checkout.run_command", recording)
    with pytest.raises(CheckoutError, match=r"^develop is the base branch, which Cleave never pushes$"):
        await co.push("develop", "0" * 40)
    assert calls == [] and git("rev-parse", "refs/heads/develop", cwd=bare) == before

# --- code-review fix wave: the push sends the commit the gates judged ---

async def test_push_sends_the_given_commit_not_a_later_one_on_the_branch(repo):
    bare, _, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    gated = git("rev-parse", "HEAD", cwd=wt)
    (wt / "late.txt").write_text("late"); git("add", ".", cwd=wt); commit(wt, "late")   # after the gates judged
    await co.push("ACME-1-fix", gated)
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == gated != git("rev-parse", "HEAD", cwd=wt)

async def test_a_commit_off_the_branchs_history_is_refused_before_anything_reaches_the_forge(repo, monkeypatch):
    bare, _, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    elsewhere = git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "HEAD^{tree}", "-m", "elsewhere",
                    cwd=wt)                                              # a commit on no branch
    calls = []
    async def recording(argv, **kwargs):
        calls.append(argv)
        return await run_command(argv, **kwargs)
    monkeypatch.setattr("orchestrator.skillflow.checkout.run_command", recording)
    with pytest.raises(CheckoutError, match=rf"^the commit {elsewhere} is not on ACME-1-fix$"):
        await co.push("ACME-1-fix", elsewhere)
    assert not any(f"file://{bare}" in argv for argv in calls)
    assert git("for-each-ref", "refs/heads/ACME-1-fix", cwd=bare) == ""

# --- the re-review: a ref named as the sha never stands in for it ---

@pytest.mark.parametrize("prefix", ["refs/tags/", "refs/heads/"])     # `git tag <sha>`, `git branch <sha>`
async def test_a_ref_named_as_the_sha_does_not_change_what_is_pushed(repo, prefix):
    bare, _, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    gated = git("rev-parse", "HEAD", cwd=wt)
    (wt / "ungated.txt").write_text("u"); git("add", ".", cwd=wt); commit(wt, "ungated")
    other = git("rev-parse", "HEAD", cwd=wt)
    git("reset", "--hard", gated, cwd=wt)
    git("update-ref", f"{prefix}{gated}", other, cwd=co.path)          # the session's ref, on another commit
    await co.push("ACME-1-fix", gated)
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == gated
    await co.push("ACME-1-fix", gated)                                  # a repeat push of the same commit
    assert git("rev-parse", "refs/heads/ACME-1-fix", cwd=bare) == gated

@pytest.mark.parametrize("name", ["short", "HEAD", "ACME-1-fix", "upper"])
async def test_a_sha_that_is_not_a_full_commit_id_is_refused_before_any_git(repo, monkeypatch, name):
    bare, _, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    sha = {"short": tip(co)[:12], "upper": tip(co).upper()}.get(name, name)
    calls = []
    async def recording(argv, **kwargs):
        calls.append(argv)
        return await run_command(argv, **kwargs)
    monkeypatch.setattr("orchestrator.skillflow.checkout.run_command", recording)
    with pytest.raises(CheckoutError, match="is not a full commit id"):
        await co.push("ACME-1-fix", sha)
    assert calls == [] and git("for-each-ref", "refs/heads/ACME-1-fix", cwd=bare) == ""

async def test_head_is_the_worktrees_commit(repo, tmp_path):
    _, _, co = repo
    await co.prepare()
    wt = add_worktree(co); (wt / "fix.txt").write_text("fix"); git("add", ".", cwd=wt); commit(wt, "fix")
    assert await co.head(wt) == git("rev-parse", "HEAD", cwd=wt)
    with pytest.raises(CheckoutError, match="git rev-parse failed"):
        await co.head(co.worktree(co.path, ".claude/worktrees/ACME-9"))
