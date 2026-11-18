"""An address job's fix: Cleave's own git turns the session's changes in the PR's worktree into one patch."""
from __future__ import annotations

import fnmatch
from pathlib import Path

from orchestrator.reviewer.commands import NO_HOST_GIT_ENV, base_env, run_command, stderr_tail

PATCH_NAME = "fix.patch"  # in the job's copy
MAX_PATCH_BYTES = 1_000_000


class FixError(Exception):
    """Why the fix was not kept; its text is the job's failure reason."""


async def _git(worktree: Path, args: list[str], timeout: float) -> str:
    """git in the worktree after its session: no host config, no hook, no fsmonitor, the PR's symlinks plain files
    as Prepare checked them out, and no walking up past the worktree's directory. The caller has checked that the
    session left the job clone's guarded git files and the worktree's .git pointer as they were."""
    argv = ["git", "-C", str(worktree), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "core.symlinks=false", *args]
    env = {**base_env(), **NO_HOST_GIT_ENV, "GIT_CEILING_DIRECTORIES": str(worktree.parent)}
    result = await run_command(argv, cwd=worktree.parent, env=env, timeout=timeout)
    if result.timed_out:
        raise FixError(f"git {args[0]} failed: timed out after {int(timeout)} s")
    if result.returncode != 0:
        raise FixError(f"git {args[0]} failed: {stderr_tail(result.stderr)}")
    return result.stdout


async def capture_fix(worktree: Path, patch: Path, protected: list[str], *, timeout: float = 300.0) -> dict:
    """{} when the worktree is unchanged (no file written), else
    {"files": [{"path": str, "added": int, "deleted": int}], "protected": [str], "bytes": int}.

    Everything in the worktree is staged and written to `patch` as a binary-safe diff against the head (no
    external diff, no textconv); a binary file counts 0 added and 0 deleted lines. A patch over MAX_PATCH_BYTES
    is deleted and raises FixError, as does a failed git command. `protected`: patterns (fnmatch) whose paths
    are listed, since such a fix is never pushed."""
    await _git(worktree, ["add", "-A"], timeout)
    numstat = await _git(worktree, ["diff", "--cached", "--numstat", "-z", "--no-renames", "HEAD"], timeout)
    files = []
    for entry in filter(None, numstat.split("\0")):
        added, deleted, path = entry.split("\t", 2)
        files.append({"path": path, "added": int(added) if added != "-" else 0,
                      "deleted": int(deleted) if deleted != "-" else 0})
    if not files:
        return {}
    await _git(worktree, ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--no-color",
                          "--no-renames", f"--output={patch}", "HEAD"], timeout)
    size = patch.stat().st_size
    if size > MAX_PATCH_BYTES:
        patch.unlink()
        raise FixError(f"the fix is too large: {size} bytes")
    hits = [f["path"] for f in files if any(fnmatch.fnmatchcase(f["path"], p) for p in protected)]
    return {"files": files, "protected": hits, "bytes": size}


def fix_summary(fix: dict) -> tuple[int, int, int]:
    """The fix's files, added lines and deleted lines."""
    files = fix.get("files", [])
    return len(files), sum(f["added"] for f in files), sum(f["deleted"] for f in files)
