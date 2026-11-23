# tests/unit/test_reviewer_clone.py
from __future__ import annotations
import asyncio, base64, os, subprocess, time
from pathlib import Path
import pytest
from orchestrator.reviewer import clone as clone_module
from orchestrator.reviewer.clone import CloneError, ReviewClone, git_env, github_clone_url
from orchestrator.reviewer.commands import CommandResult

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

def commit_and_push(work, name):
    (work / name).write_text(name)
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", name, cwd=work)
    git("push", "origin", "HEAD:develop", cwd=work)
    return git("rev-parse", "HEAD", cwd=work)

@pytest.fixture
def origin(tmp_path):
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    git("init", "--bare", "-b", "develop", str(bare))
    git("clone", str(bare), str(work))
    (work / "docs").mkdir()
    (work / "docs" / "process.md").write_text("# Process\n")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "a", cwd=work)
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "b", cwd=work)
    git("push", "origin", "HEAD:develop", cwd=work)
    return bare, work

@pytest.fixture
def pr7(tmp_path, origin):
    """origin plus PR 7: one commit on top of develop, pushed to refs/pull/7/head as GitHub keeps it."""
    bare, work = origin
    pr_work = tmp_path / "pr-work"
    git("clone", str(bare), str(pr_work))
    (pr_work / "feature.txt").write_text("feature")
    git("add", ".", cwd=pr_work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "pr", cwd=pr_work)
    git("push", "origin", "HEAD:refs/pull/7/head", cwd=pr_work)
    return bare, git("rev-parse", "HEAD", cwd=work), git("rev-parse", "HEAD", cwd=pr_work)

async def refreshed(tmp_path, bare):
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", "develop", "tok")
    await clone.refresh()
    return clone

def test_git_env_carries_token_only_as_header(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "leak")
    env = git_env("tok")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_VALUE_1"] == "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:tok").decode()
    assert "GH_TOKEN" not in env
    assert env["GIT_CONFIG_COUNT"] == "6"
    assert (env["GIT_CONFIG_KEY_2"], env["GIT_CONFIG_VALUE_2"]) == ("maintenance.autoDetach", "false")
    assert (env["GIT_CONFIG_KEY_3"], env["GIT_CONFIG_VALUE_3"]) == ("gc.autoDetach", "false")
    assert (env["GIT_CONFIG_KEY_4"], env["GIT_CONFIG_VALUE_4"]) == ("core.hooksPath", "/dev/null")
    assert (env["GIT_CONFIG_KEY_5"], env["GIT_CONFIG_VALUE_5"]) == ("core.fsmonitor", "false")

async def test_refresh_runs_no_planted_hook_or_fsmonitor(tmp_path, origin):
    # Environment config outranks .git/config: nothing planted in the clone runs in a git call with the token.
    bare, work = origin
    clone = await refreshed(tmp_path, bare)
    git_dir = tmp_path / "clone" / ".git"
    for name, script, status in (("hook", git_dir / "hooks" / "post-checkout", 0),
                                 ("fsmonitor", tmp_path / "fsmonitor.sh", 1)):
        script.write_text(f"#!/bin/sh\ntouch {tmp_path / name}-ran\nexit {status}\n")
        script.chmod(0o755)
    with (git_dir / "config").open("a") as f:
        f.write(f"[core]\n\tfsmonitor = {tmp_path / 'fsmonitor.sh'}\n")
    await clone.refresh()
    assert not (tmp_path / "hook-ran").exists() and not (tmp_path / "fsmonitor-ran").exists()

def test_git_env_empties_global_helper(tmp_path, monkeypatch):
    (tmp_path / ".gitconfig").write_text("[credential]\n\thelper = osxkeychain\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    out = subprocess.run(["git", "config", "--get-all", "credential.helper"], env=git_env("tok"),
                         capture_output=True, text=True).stdout.splitlines()
    assert out[-1] == ""          # the last value wins: helpers reset

async def test_refresh_clones_then_fetches(tmp_path, origin):
    bare, work = origin
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", "develop", "tok")
    sha = await clone.refresh()
    assert sha == git("rev-parse", "HEAD", cwd=work) and clone.has_file("docs/process.md")
    assert "tok" not in (tmp_path / "clone" / ".git" / "config").read_text()
    (work / "new.txt").write_text("x")
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "c", cwd=work)
    git("push", "origin", "HEAD:develop", cwd=work)
    assert await clone.refresh() == git("rev-parse", "HEAD", cwd=work)
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=tmp_path / "clone") == "HEAD"   # detached

async def test_clone_error_is_redacted(tmp_path, origin):
    bare, _ = origin
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", base_branch="SECRET-branch", token="SECRET")
    with pytest.raises(CloneError) as e:
        await clone.refresh()
    assert str(e.value).startswith("git clone failed: ") and "***-branch" in str(e.value)
    assert "SECRET" not in str(e.value)

def test_git_env_ignores_global_config(tmp_path, monkeypatch):
    (tmp_path / ".gitconfig").write_text("[core]\n\taskPass = /usr/bin/false\n"
                                         '[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    def config(*args, env):
        return subprocess.run(["git", "config", *args], cwd=tmp_path, env=env, capture_output=True, text=True).stdout
    plain = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    assert config("--get", "core.askpass", env=plain).strip() == "/usr/bin/false"   # the fixture is read
    env = git_env("tok")
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_ASKPASS"] == ""
    assert config("--get", "core.askpass", env=env) == ""
    assert config("--get-regexp", r"^url\.", env=env) == ""

async def test_refresh_overwrites_untracked_file_the_base_now_tracks(tmp_path, origin):
    bare, work = origin
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", "develop", "tok")
    await clone.refresh()
    (tmp_path / "clone" / "u.txt").write_text("stray")
    head = commit_and_push(work, "u.txt")
    assert await clone.refresh() == head

async def test_refresh_removes_stale_locks(tmp_path, origin):
    bare, work = origin
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", "develop", "tok")
    await clone.refresh()
    git("worktree", "add", "--detach", str(tmp_path / "wt"), cwd=tmp_path / "clone")
    git_dir = tmp_path / "clone" / ".git"
    (git_dir / "refs" / "remotes" / "origin").mkdir(parents=True, exist_ok=True)
    for lock in ("index.lock", "refs/remotes/origin/develop.lock", "worktrees/wt/index.lock"):
        (git_dir / lock).write_text("")
    head = commit_and_push(work, "new.txt")
    assert await clone.refresh() == head
    assert (git_dir / "worktrees" / "wt" / "index.lock").exists()   # a worktree's lock is left alone

async def test_refresh_leaves_locks_under_objects_alone(tmp_path, origin):
    bare, _ = origin
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", "develop", "tok")
    await clone.refresh()
    lock = tmp_path / "clone" / ".git" / "objects" / "stray.lock"
    lock.write_text("")
    await clone.refresh()
    assert lock.exists()

async def test_refresh_follows_a_changed_url(tmp_path, origin):
    bare_a, _ = origin
    bare_b, work_b = tmp_path / "b.git", tmp_path / "work-b"
    git("init", "--bare", "-b", "develop", str(bare_b))
    git("clone", str(bare_b), str(work_b))
    head_b = commit_and_push(work_b, "only-in-b.txt")
    path = tmp_path / "clone"
    await ReviewClone(path, f"file://{bare_a}", "develop", "tok").refresh()
    moved = ReviewClone(path, f"file://{bare_b}", "develop", "tok")
    assert await moved.refresh() == head_b
    assert moved.has_file("only-in-b.txt") and not moved.has_file("docs/process.md")
    assert git("config", "--get", "remote.origin.url", cwd=path) == f"file://{bare_b}"
    assert "tok" not in (path / ".git" / "config").read_text()

async def test_refresh_returns_only_after_auto_maintenance(tmp_path, origin, monkeypatch):
    bare, work = origin
    # The probe is a hook, and git_env turns hooks off (its last two entries): here git reads only the
    # first four, the maintenance ones included.
    real_env = clone_module.git_env
    monkeypatch.setattr(clone_module, "git_env", lambda token: {**real_env(token), "GIT_CONFIG_COUNT": "4"})
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", "develop", "tok")
    await clone.refresh()
    # Make the next fetch's auto maintenance run gc: the gc strategy (git's default is heuristic),
    # a fetch that keeps its own pack, and a pack limit that a second pack exceeds.
    for key, value in (("maintenance.strategy", "gc"), ("fetch.unpackLimit", "1"), ("gc.autoPackLimit", "1")):
        git("config", key, value, cwd=tmp_path / "clone")
    runs = tmp_path / "gc-hook-runs"   # gc --auto runs this hook, slowly, each time it checks whether to run
    hook = tmp_path / "clone" / ".git" / "hooks" / "pre-auto-gc"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\nsleep 0.5\necho run >> '{runs}'\n")
    hook.chmod(0o755)
    commit_and_push(work, "new.txt")
    await clone.refresh()
    seen = runs.read_text() if runs.exists() else ""
    time.sleep(1.5)
    assert seen and runs.read_text() == seen   # maintenance ran, and none of it was still running on .git

async def test_broken_clone_never_reaches_enclosing_repo(tmp_path, origin):
    bare, work = origin
    (work / "docs" / "process.md").write_text("uncommitted edit\n")
    (work / "sub" / "clone" / ".git").mkdir(parents=True)
    clone = ReviewClone(work / "sub" / "clone", f"file://{bare}", "develop", "tok")
    with pytest.raises(CloneError):
        await clone.refresh()
    assert (work / "docs" / "process.md").read_text() == "uncommitted edit\n"

async def test_relative_path_is_made_absolute(tmp_path, origin, monkeypatch):
    bare, work = origin
    monkeypatch.chdir(tmp_path)
    clone = ReviewClone(Path("rel") / "clone", f"file://{bare}", "develop", "tok")
    await clone.refresh()
    assert await clone.refresh() == git("rev-parse", "HEAD", cwd=work) and clone.has_file("docs/process.md")

async def test_refresh_keeps_local_base_branch_current(tmp_path, origin):
    bare, work = origin
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", "develop", "tok")
    await clone.refresh()
    head = commit_and_push(work, "new.txt")
    await clone.refresh()
    assert git("rev-parse", "develop", cwd=tmp_path / "clone") == head

def test_github_clone_url():
    assert github_clone_url("acme-org", "acme-app") == "https://github.com/acme-org/acme-app.git"

async def test_fetch_pr_head_makes_local_branch(tmp_path, pr7):
    bare, _, head = pr7
    clone = await refreshed(tmp_path, bare)
    assert await clone.fetch_pr_head(7) == head
    assert git("rev-parse", "pr-review/7", cwd=clone.path) == head
    assert "tok" not in (clone.path / ".git" / "config").read_text()

async def test_merge_base(tmp_path, origin, pr7):
    _, work = origin
    bare, base, _ = pr7
    commit_and_push(work, "later.txt")   # develop moves on after the PR forked
    clone = await refreshed(tmp_path, bare)
    head = await clone.fetch_pr_head(7)
    assert await clone.merge_base(head) == base

async def test_add_and_remove_worktree(tmp_path, pr7):
    bare, _, head = pr7
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_pr_head(7)
    wt = tmp_path / "wt"
    await clone.add_worktree(wt, head)
    assert (wt / "feature.txt").read_text() == "feature"
    assert git("rev-parse", "HEAD", cwd=wt) == head
    assert await clone.remove_worktree(wt) == "" and not wt.exists()
    assert await clone.remove_worktree(wt) == ""   # no longer registered

async def test_remove_worktree_refuses_with_changes(tmp_path, pr7):
    bare, _, head = pr7
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_pr_head(7)
    wt = tmp_path / "wt"
    await clone.add_worktree(wt, head)
    (wt / "stray.txt").write_text("x")
    assert await clone.remove_worktree(wt) != ""
    assert (wt / "stray.txt").exists()

async def test_add_worktree_replaces_registered_leftover(tmp_path, pr7):
    bare, base, head = pr7
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_pr_head(7)
    wt = tmp_path / "wt"
    await clone.add_worktree(wt, base)
    await clone.add_worktree(wt, head)
    assert git("rev-parse", "HEAD", cwd=wt) == head

async def test_add_worktree_refuses_leftover_with_changes(tmp_path, pr7):
    bare, base, head = pr7
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_pr_head(7)
    wt = tmp_path / "wt"
    await clone.add_worktree(wt, base)
    (wt / "stray.txt").write_text("x")
    with pytest.raises(CloneError) as e:
        await clone.add_worktree(wt, head)
    assert str(e.value).startswith(f"worktree path in use: {wt} — ")
    assert (wt / "stray.txt").exists() and git("rev-parse", "HEAD", cwd=wt) == base

async def test_add_worktree_rejects_foreign_path(tmp_path, pr7):
    bare, _, head = pr7
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_pr_head(7)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "keep.txt").write_text("x")
    with pytest.raises(CloneError, match="is not a worktree"):
        await clone.add_worktree(wt, head)
    assert (wt / "keep.txt").exists()

async def test_pr_symlinks_check_out_as_plain_files(tmp_path, origin):
    bare, _ = origin
    pr_work = tmp_path / "pr-work"
    git("clone", str(bare), str(pr_work))
    (pr_work / "hosts-link").symlink_to("/etc/hosts")
    (pr_work / "up").symlink_to("..", target_is_directory=True)
    git("add", ".", cwd=pr_work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "links", cwd=pr_work)
    git("push", "origin", "HEAD:refs/pull/8/head", cwd=pr_work)
    clone = await refreshed(tmp_path, bare)
    head = await clone.fetch_pr_head(8)
    wt = tmp_path / "wt"
    await clone.add_worktree(wt, head)
    for name, target in (("hosts-link", "/etc/hosts"), ("up", "..")):
        assert not (wt / name).is_symlink() and (wt / name).read_text() == target
    assert await clone.remove_worktree(wt) == "" and not wt.exists()   # the plain files are no change

async def test_worktree_error_is_redacted(tmp_path, monkeypatch):
    wt = tmp_path / "wt-SECRET"
    async def failing(argv, **kwargs):
        return CommandResult(returncode=128, stdout="", stderr=f"fatal: cannot lock '{wt}'\n", timed_out=False)
    monkeypatch.setattr("orchestrator.reviewer.clone.run_command", failing)
    clone = ReviewClone(tmp_path / "clone", "file:///nowhere", "develop", token="SECRET")
    with pytest.raises(CloneError) as e:
        await clone.add_worktree(wt, "a" * 40)
    assert "***" in str(e.value) and "SECRET" not in str(e.value)
    reason = await clone.remove_worktree(wt)
    assert "***" in reason and "SECRET" not in reason
    assert clone.redact("x SECRET y") == "x *** y"

def idle_clone(tmp_path):
    return ReviewClone(tmp_path / "clone", "file:///nowhere", "develop", "tok")

async def test_reader_takes_free_lock(tmp_path):
    clone = idle_clone(tmp_path)
    async with clone.reader():
        assert clone.lock.locked()
    assert not clone.lock.locked()

async def test_reader_waits_for_a_short_lock(tmp_path):
    """A job's Prepare holds the lock for its git there only: a post waits for it, then runs."""
    clone = idle_clone(tmp_path)
    await clone.lock.acquire()
    asyncio.get_running_loop().call_later(0.05, clone.lock.release)
    async with clone.reader(wait=5):
        assert clone.lock.locked()
    assert not clone.lock.locked()

async def test_reader_busy_past_its_wait(tmp_path):
    clone = idle_clone(tmp_path)
    await clone.lock.acquire()
    with pytest.raises(CloneError, match="^the review clone is in use; try again in a minute$"):
        async with clone.reader(wait=0.05):
            pass
    assert clone.lock.locked()   # still the holder's
    clone.lock.release()
    for _ in range(3):
        await asyncio.sleep(0)
    assert not clone.lock.locked()   # no waiter of the reader's left behind to take it

@pytest.mark.parametrize("granted", [False, True], ids=["waiting", "granted-in-the-same-step"])
async def test_reader_cancelled_on_lock_leaves_it_free(tmp_path, granted):
    clone, queued = idle_clone(tmp_path), asyncio.Event()
    await clone.lock.acquire()   # a job prepares
    async def post():
        queued.set()
        async with clone.reader(wait=10):
            await asyncio.Event().wait()
    task = asyncio.create_task(post())
    await queued.wait()
    for _ in range(3):
        await asyncio.sleep(0)   # the reader waits on the lock now
    if granted:
        clone.lock.release()     # handed to the reader's waiter...
    task.cancel()                # ...which is cancelled before it runs
    with pytest.raises(asyncio.CancelledError):
        await task
    if not granted:
        clone.lock.release()
    for _ in range(3):
        await asyncio.sleep(0)
    assert not clone.lock.locked()

def test_no_session_phase_on_the_review_clone(tmp_path):
    clone = idle_clone(tmp_path)
    assert not any(hasattr(clone, name) for name in ("session_phase", "session_lock", "in_session",
                                                     "changed_git_files"))

async def test_delete_branch(tmp_path, pr7):
    bare, base, _ = pr7
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_pr_head(7)
    await clone.delete_branch(7)
    with pytest.raises(subprocess.CalledProcessError):
        git("rev-parse", "--verify", "pr-review/7", cwd=clone.path)
    await clone.delete_branch(7)   # not there: no error
    git("symbolic-ref", "refs/heads/pr-review/7", "refs/heads/develop", cwd=clone.path)
    await clone.delete_branch(7)   # the link goes, never the branch it points at
    with pytest.raises(subprocess.CalledProcessError):
        git("symbolic-ref", "-q", "refs/heads/pr-review/7", cwd=clone.path)
    assert git("rev-parse", "--verify", "refs/heads/develop", cwd=clone.path) == base


async def job_clone(tmp_path, bare, number=7):
    """The review clone refreshed with PR `number` fetched, and a job clone made from it."""
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_pr_head(number)
    async with clone.lock:
        job = await clone.make_job_clone(tmp_path / "work" / "acme" / "app" / "job1" / "clone", number)
    await job.detach_at_base()   # outside the lock: the job clone is the job's own
    return clone, job


async def test_job_clone_is_at_base_with_the_pr_branch(tmp_path, pr7):
    bare, base, head = pr7
    _, job = await job_clone(tmp_path, bare)
    assert git("rev-parse", "HEAD", cwd=job.path) == base
    assert git("rev-parse", "refs/heads/pr-review/7", cwd=job.path) == head
    assert git("rev-parse", "refs/heads/develop", cwd=job.path) == base
    assert git("rev-parse", "refs/remotes/origin/develop", cwd=job.path) == base
    assert await job.merge_base(head) == base
    assert (job.path / "docs" / "process.md").is_file()


async def test_job_clone_needs_nothing_from_the_review_clone(tmp_path, pr7):
    bare, _, head = pr7
    clone, job = await job_clone(tmp_path, bare)
    assert not (job.path / ".git" / "objects" / "info" / "alternates").exists()
    await clone.delete_branch(7)
    git("-c", "gc.reflogExpire=now", "-c", "gc.reflogExpireUnreachable=now", "gc", "--prune=now", cwd=clone.path)
    git("cat-file", "-e", head, cwd=job.path)  # the PR head's objects are the job clone's own


async def test_job_clone_carries_no_token(tmp_path, pr7):
    bare, _, _ = pr7
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", "develop", "ghp_job_clone_secret")
    await clone.refresh()
    await clone.fetch_pr_head(7)
    async with clone.lock:
        job = await clone.make_job_clone(tmp_path / "work" / "job1" / "clone", 7)
    config = (job.path / ".git" / "config").read_text()
    assert "extraheader" not in config and "credential" not in config and "ghp_job_clone_secret" not in config
    assert git("config", "--get", "remote.origin.url", cwd=job.path) == str(clone.path)


async def test_job_clone_refuses_an_existing_path(tmp_path, pr7):
    bare, _, _ = pr7
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_pr_head(7)
    dest = tmp_path / "taken"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine")
    with pytest.raises(CloneError, match="job clone path exists"):
        async with clone.lock:
            await clone.make_job_clone(dest, 7)
    assert (dest / "keep.txt").read_text() == "mine"


async def test_job_clone_has_no_checkout_until_detached(tmp_path, pr7):
    bare, base, _ = pr7
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_pr_head(7)
    async with clone.lock:
        job = await clone.make_job_clone(tmp_path / "work" / "job1" / "clone", 7)
    assert not (job.path / "docs").exists()   # the review clone's lock covered no checkout
    await job.detach_at_base()
    assert git("rev-parse", "HEAD", cwd=job.path) == base and (job.path / "docs" / "process.md").is_file()


# --- Address jobs: the push of a fix ---

AUTHOR = ("Cleave Bot", "cleave@pipeline.local")
MESSAGE = "fix: address review comments on #7"

def pr_branch_and_patch(tmp_path, bare, head):
    """PR 7's branch feature/pr7 at its head on origin, and a patch (made with git diff --binary in a scratch
    checkout of the head) that changes feature.txt and adds a binary file."""
    pr_work = tmp_path / "pr-work"
    git("push", str(bare), f"{head}:refs/heads/feature/pr7", cwd=pr_work)
    scratch = tmp_path / "scratch"
    git("clone", "--quiet", str(bare), str(scratch))
    git("checkout", "--quiet", "--detach", head, cwd=scratch)
    (scratch / "feature.txt").write_text("feature, fixed\n")
    (scratch / "blob.bin").write_bytes(b"\x00\xff\xfe binary")
    git("add", "-A", cwd=scratch)
    patch = tmp_path / "fix.patch"
    git("diff", "--cached", "--binary", f"--output={patch}", "HEAD", cwd=scratch)
    return patch

async def push(clone, tmp_path, head, patch, branch="feature/pr7"):
    async with clone.lock:
        return await clone.push_patch(number=7, head=head, branch=branch, patch=patch, message=MESSAGE,
                                      author=AUTHOR, worktree=tmp_path / "push" / "wt")

def move_pr(tmp_path, bare, refs):
    """One more commit on the PR's head, pushed to `refs` of origin; its sha."""
    pr_work = tmp_path / "pr-work"
    (pr_work / "laptop.txt").write_text("from the laptop")
    git("add", ".", cwd=pr_work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "laptop", cwd=pr_work)
    for ref in refs:
        git("push", "--quiet", str(bare), f"HEAD:{ref}", cwd=pr_work)
    return git("rev-parse", "HEAD", cwd=pr_work)

async def test_push_patch_adds_one_commit_to_the_branch(tmp_path, pr7):
    bare, _, head = pr7
    patch = pr_branch_and_patch(tmp_path, bare, head)
    clone = await refreshed(tmp_path, bare)
    sha = await push(clone, tmp_path, head, patch)
    assert len(sha) == 40
    assert git("rev-parse", "feature/pr7", cwd=bare) == sha and git("rev-parse", f"{sha}^", cwd=bare) == head
    assert git("log", "-1", "--format=%an <%ae>|%s", sha, cwd=bare) == f"Cleave Bot <cleave@pipeline.local>|{MESSAGE}"
    assert git("show", f"{sha}:feature.txt", cwd=bare) == "feature, fixed"
    blob = subprocess.run(["git", "show", f"{sha}:blob.bin"], cwd=bare, check=True, capture_output=True).stdout
    assert blob == b"\x00\xff\xfe binary"
    assert git("diff", "--name-only", head, sha, cwd=bare).split() == ["blob.bin", "feature.txt"]

async def test_push_patch_refuses_a_moved_head(tmp_path, pr7):
    bare, _, head = pr7
    patch = pr_branch_and_patch(tmp_path, bare, head)
    moved = move_pr(tmp_path, bare, ["refs/pull/7/head", "refs/heads/feature/pr7"])
    clone = await refreshed(tmp_path, bare)
    with pytest.raises(CloneError, match="head moved"):
        await push(clone, tmp_path, head, patch)
    assert git("rev-parse", "feature/pr7", cwd=bare) == moved

async def test_push_patch_refuses_a_non_fast_forward(tmp_path, pr7):
    # The branch moved, GitHub's pull ref not yet: the push itself is rejected, never forced.
    bare, _, head = pr7
    patch = pr_branch_and_patch(tmp_path, bare, head)
    moved = move_pr(tmp_path, bare, ["refs/heads/feature/pr7"])
    clone = await refreshed(tmp_path, bare)
    with pytest.raises(CloneError, match="^git push failed: "):
        await push(clone, tmp_path, head, patch)
    assert git("rev-parse", "feature/pr7", cwd=bare) == moved

async def test_push_patch_refuses_a_branch_set_back(tmp_path, pr7):
    # The branch was reset to the head's parent, GitHub's pull ref not yet: a plain push would fast-forward it and
    # bring the dropped commit back. The push expects the branch at the job's head and is refused.
    bare, _, head = pr7
    patch = pr_branch_and_patch(tmp_path, bare, head)
    parent = git("rev-parse", f"{head}^", cwd=bare)
    git("update-ref", "refs/heads/feature/pr7", parent, cwd=bare)
    clone = await refreshed(tmp_path, bare)
    with pytest.raises(CloneError, match="^git push failed: "):
        await push(clone, tmp_path, head, patch)
    assert git("rev-parse", "feature/pr7", cwd=bare) == parent

async def test_push_patch_never_forces(tmp_path, pr7, monkeypatch):
    bare, _, head = pr7
    patch = pr_branch_and_patch(tmp_path, bare, head)
    clone = await refreshed(tmp_path, bare)
    argvs, real = [], clone_module.run_command
    async def recording(argv, **kw):
        argvs.append(argv)
        return await real(argv, **kw)
    monkeypatch.setattr(clone_module, "run_command", recording)
    await push(clone, tmp_path, head, patch)
    pushes = [argv for argv in argvs if "push" in argv]
    assert len(pushes) == 1 and pushes[0][-1] == f"{git('rev-parse', 'feature/pr7', cwd=bare)}^{{commit}}:refs/heads/feature/pr7"
    # The one lease names the branch and the head it must be at; never a bare one, which trusts a tracking ref.
    assert [arg for arg in pushes[0] if arg.startswith("--force")] == [f"--force-with-lease=refs/heads/feature/pr7:{head}"]
    for argv in argvs:
        assert not {"--force", "-f", "--force-with-lease", "--mirror"} & set(argv)
    assert not any(arg.startswith("+") for arg in pushes[0])

async def test_push_patch_runs_no_planted_hook(tmp_path, pr7):
    bare, _, head = pr7
    patch = pr_branch_and_patch(tmp_path, bare, head)
    clone = await refreshed(tmp_path, bare)
    for name in ("pre-commit", "commit-msg", "post-commit", "pre-push", "post-checkout"):
        hook = clone.path / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ntouch {tmp_path / name}-ran\n")
        hook.chmod(0o755)
    await push(clone, tmp_path, head, patch)
    assert not list(tmp_path.glob("*-ran"))

async def test_push_patch_refuses_a_patch_that_does_not_apply(tmp_path, pr7):
    bare, _, head = pr7
    pr_branch_and_patch(tmp_path, bare, head)
    bad = tmp_path / "bad.patch"
    bad.write_text("diff --git a/nope.txt b/nope.txt\n--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-a\n+b\n")
    clone = await refreshed(tmp_path, bare)
    with pytest.raises(CloneError, match="^git apply failed: "):
        await push(clone, tmp_path, head, bad)
    assert git("rev-parse", "feature/pr7", cwd=bare) == head


@pytest.fixture
def stacked(tmp_path, origin):
    """origin plus task/parent (one commit on develop) and PR 8 on top of it at refs/pull/8/head; develop then moves
    on. Returns (bare, work, parent tip, PR 8 head)."""
    bare, work = origin
    git("checkout", "-b", "task/parent", cwd=work)
    (work / "parent.txt").write_text("parent")
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "parent", cwd=work)
    git("push", "origin", "HEAD:task/parent", cwd=work)
    parent = git("rev-parse", "HEAD", cwd=work)
    (work / "child.txt").write_text("child")
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "child", cwd=work)
    git("push", "origin", "HEAD:refs/pull/8/head", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work)
    git("checkout", "--detach", parent + "~1", cwd=work)
    commit_and_push(work, "later.txt")  # develop moves on after both forked
    return bare, work, parent, head


async def test_fetch_branch_and_merge_base_against_it(tmp_path, stacked):
    bare, _, parent, head = stacked
    clone = await refreshed(tmp_path, bare)
    assert await clone.fetch_branch("task/parent") == parent
    assert git("rev-parse", "task/parent", cwd=clone.path) == parent
    assert await clone.fetch_pr_head(8) == head
    assert await clone.merge_base(head, "task/parent") == parent
    assert await clone.merge_base(head) == git("rev-parse", "develop~1", cwd=clone.path)  # the configured base
    async with clone.lock:
        job = await clone.make_job_clone(tmp_path / "work" / "job1" / "clone", 8)
    await job.detach_at_base()
    assert git("rev-parse", "HEAD", cwd=job.path) == git("rev-parse", "develop", cwd=clone.path)  # the trusted checkout
    assert await job.merge_base(head, "task/parent") == parent


async def test_fetch_branch_follows_the_tip(tmp_path, stacked):
    bare, work, parent, _ = stacked
    clone = await refreshed(tmp_path, bare)
    await clone.fetch_branch("task/parent")
    git("checkout", "--detach", parent, cwd=work)
    (work / "more.txt").write_text("more")
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "more", cwd=work)
    git("push", "origin", "HEAD:task/parent", cwd=work)
    assert await clone.fetch_branch("task/parent") == git("rev-parse", "HEAD", cwd=work)


@pytest.mark.parametrize("name", ["-x", "a..b", "pr-review/7", "bad name", "x/", "x.lock", "", "a:b", "refs/heads/x"])
async def test_fetch_branch_refuses_odd_names(tmp_path, stacked, name):
    bare, *_ = stacked
    clone = await refreshed(tmp_path, bare)
    with pytest.raises(CloneError, match="is not a branch Cleave fetches"):
        await clone.fetch_branch(name)


async def test_fetch_branch_of_a_gone_branch_is_a_clone_error(tmp_path, stacked):
    bare, *_ = stacked
    clone = await refreshed(tmp_path, bare)
    with pytest.raises(CloneError):
        await clone.fetch_branch("task/gone")
