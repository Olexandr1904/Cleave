# tests/unit/test_reviewer_clone.py
from __future__ import annotations
import base64, os, subprocess, time
from pathlib import Path
import pytest
from orchestrator.reviewer.clone import CloneError, ReviewClone, git_env, github_clone_url

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

def test_git_env_carries_token_only_as_header(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "leak")
    env = git_env("tok")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_VALUE_1"] == "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:tok").decode()
    assert "GH_TOKEN" not in env
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert (env["GIT_CONFIG_KEY_2"], env["GIT_CONFIG_VALUE_2"]) == ("maintenance.autoDetach", "false")
    assert (env["GIT_CONFIG_KEY_3"], env["GIT_CONFIG_VALUE_3"]) == ("gc.autoDetach", "false")

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

async def test_refresh_returns_only_after_auto_maintenance(tmp_path, origin):
    bare, work = origin
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
