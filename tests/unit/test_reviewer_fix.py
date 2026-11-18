# tests/unit/test_reviewer_fix.py
from __future__ import annotations
import os, subprocess
import pytest
from orchestrator.reviewer import fix as fix_module
from orchestrator.reviewer.fix import (
    MAX_PATCH_BYTES, PATCH_NAME, FixError, capture_fix, fix_summary, require_clean,
)

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

@pytest.fixture
def repo(tmp_path):
    """A repo with one commit holding a.txt, .github/ci.yml and a symlink link -> a.txt."""
    repo = tmp_path / "repo"
    git("init", "-b", "develop", str(repo))
    (repo / "a.txt").write_text("a\n")
    (repo / ".github").mkdir()
    (repo / ".github" / "ci.yml").write_text("ci\n")
    os.symlink("a.txt", repo / "link")
    git("add", ".", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "a", cwd=repo)
    return repo

@pytest.fixture
def wt(tmp_path, repo):
    """The PR worktree as Prepare checks it out: symlinks as plain files."""
    git("-c", "core.symlinks=false", "worktree", "add", "--detach", str(tmp_path / "wt"), "HEAD", cwd=repo)
    return tmp_path / "wt"

def test_names():
    assert (PATCH_NAME, MAX_PATCH_BYTES) == ("fix.patch", 1_000_000)

async def test_capture_and_apply_roundtrip_with_a_binary_file(repo, wt, tmp_path):
    (wt / "a.txt").write_text("changed\n"); (wt / "new.bin").write_bytes(b"\x00\xff\xfe binary")
    fix = await capture_fix(wt, tmp_path / "fix.patch", [".github/**"])
    assert sorted(f["path"] for f in fix["files"]) == ["a.txt", "new.bin"] and fix["protected"] == []
    assert fix["bytes"] == (tmp_path / "fix.patch").stat().st_size > 0
    assert {f["path"]: (f["added"], f["deleted"]) for f in fix["files"]} == {"a.txt": (1, 1), "new.bin": (0, 0)}
    other = tmp_path / "other"; git("worktree", "add", "--detach", str(other), "HEAD", cwd=repo)
    git("apply", "--index", "--binary", str(tmp_path / "fix.patch"), cwd=other)
    assert (other / "new.bin").read_bytes() == b"\x00\xff\xfe binary" and (other / "a.txt").read_text() == "changed\n"

async def test_capture_without_changes_writes_nothing(wt, tmp_path):
    assert await capture_fix(wt, tmp_path / "fix.patch", []) == {} and not (tmp_path / "fix.patch").exists()

async def test_capture_ignores_plain_file_symlinks(wt, tmp_path):   # `link` is a plain file in wt
    assert (wt / "link").is_file() and not (wt / "link").is_symlink()
    (wt / "a.txt").write_text("changed\n")
    assert [f["path"] for f in (await capture_fix(wt, tmp_path / "p", []))["files"]] == ["a.txt"]

async def test_capture_lists_protected_paths(wt, tmp_path):
    (wt / ".github" / "ci.yml").write_text("evil\n")
    (wt / "a.txt").write_text("changed\n")
    assert (await capture_fix(wt, tmp_path / "p", [".github/**"]))["protected"] == [".github/ci.yml"]

async def test_capture_runs_no_planted_hook(repo, wt, tmp_path):
    hook = repo / ".git" / "hooks" / "post-index-change"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path / 'ran'}\n"); hook.chmod(0o755)
    (wt / "a.txt").write_text("changed\n")
    await capture_fix(wt, tmp_path / "p", [])
    assert not (tmp_path / "ran").exists()

async def test_capture_refuses_a_huge_patch(wt, tmp_path):
    (wt / "big.txt").write_text("x\n" * 600_000)
    with pytest.raises(FixError, match="too large"):
        await capture_fix(wt, tmp_path / "p", [])
    assert not (tmp_path / "p").exists()

async def test_capture_removes_an_earlier_runs_patch(wt, tmp_path):
    (tmp_path / "fix.patch").write_text("an earlier run's")
    assert await capture_fix(wt, tmp_path / "fix.patch", []) == {} and not (tmp_path / "fix.patch").exists()

@pytest.mark.parametrize("as_file", [False, True], ids=["directory", "gitfile"])
async def test_capture_refuses_a_nested_git_repository_before_git_runs(wt, tmp_path, monkeypatch, as_file):
    # A `.git` the session wrote below the worktree: git would stage a link to its commit, or run a git of its own
    # in it under the config written there. Refused before any git runs.
    sub = wt / "vendor" / "lib"
    sub.mkdir(parents=True)
    if as_file:
        (sub / ".git").write_text("gitdir: /elsewhere\n")
    else:
        (sub / ".git").mkdir()
        (sub / ".git" / "config").write_text("[core]\n\tfsmonitor = touch /tmp/ran\n")
    (wt / "a.txt").write_text("changed\n")
    ran = []
    async def no_git(argv, **kw):
        ran.append(argv)
    monkeypatch.setattr(fix_module, "run_command", no_git)
    with pytest.raises(FixError, match="^the fix holds a nested git repository: vendor/lib$"):
        await capture_fix(wt, tmp_path / "p", [])
    assert ran == [] and not (tmp_path / "p").exists()

async def test_require_clean(wt):
    await require_clean(wt)                      # the head as Prepare checks it out, its symlink a plain file
    (wt / "junk.txt").write_text("left by a prepare step\n")
    with pytest.raises(FixError, match="^Prepare left changes in the worktree: junk.txt$"):
        await require_clean(wt)

async def test_a_failed_git_command_is_a_fix_error(tmp_path):
    (tmp_path / "not-a-repo").mkdir()
    with pytest.raises(FixError, match="^git add failed: "):
        await capture_fix(tmp_path / "not-a-repo", tmp_path / "p", [])

def test_fix_summary():
    assert fix_summary({"files": [{"path": "a", "added": 3, "deleted": 1}, {"path": "b", "added": 0, "deleted": 0}]}) == (2, 3, 1)
    assert fix_summary({}) == (0, 0, 0)
