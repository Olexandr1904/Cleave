"""The review clone: the daemon's own checkout of a reviewed repo, kept at the base branch."""
from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from orchestrator.reviewer.commands import NO_HOST_GIT_ENV, base_env, redact, run_command, stderr_tail

# Where a setting planted in the clone makes git run a program: .git/config (helpers, filter, diff and merge
# drivers), .git/info/attributes (the files a driver applies to) and .git/commondir (which sends git to another
# directory's config). A job saves them before its session.
_GUARDED_GIT_FILES = ("config", "info/attributes", "commondir")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def git_env(token: str) -> dict[str, str]:
    """base_env() plus git config that resets every credential helper and sends the token as a header.

    The token lives only in this environment: never in .git/config, never in the keychain. System and
    global config are not read at all, so no askPass, helper or insteadOf from the host can apply.
    Auto maintenance stays on but runs in the foreground, so nothing touches .git after a call returns.
    Hooks and fsmonitor are off (environment config outranks .git/config); the rest of .git/config is the
    clone's own, which no session ever touches (sessions run in job clones, removed after their job).
    """
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **base_env(),
        **NO_HOST_GIT_ENV,
        # Beyond the shared base: these calls reach the network, so no askPass program either.
        "GIT_ASKPASS": "",
        "GIT_CONFIG_COUNT": "6",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_1": f"AUTHORIZATION: basic {header}",
        "GIT_CONFIG_KEY_2": "maintenance.autoDetach",
        "GIT_CONFIG_VALUE_2": "false",
        "GIT_CONFIG_KEY_3": "gc.autoDetach",
        "GIT_CONFIG_VALUE_3": "false",
        "GIT_CONFIG_KEY_4": "core.hooksPath",
        "GIT_CONFIG_VALUE_4": "/dev/null",
        "GIT_CONFIG_KEY_5": "core.fsmonitor",
        "GIT_CONFIG_VALUE_5": "false",
    }


def guarded_git_files(clone_dir: Path) -> dict[Path, bytes | None]:
    """The clone's guarded git files and their bytes; None for one that is absent."""
    git_dir = clone_dir / ".git"
    return {path: path.read_bytes() if path.exists() else None
            for path in (git_dir / name for name in _GUARDED_GIT_FILES)}


def git_file_changed(path: Path, saved: bytes | None) -> bool:
    """Whether the file at path no longer holds `saved` (None: whether something is there now); a symlink in
    its place always counts as a change."""
    if saved is None:
        return path.is_symlink() or path.exists()
    try:
        return path.is_symlink() or path.read_bytes() != saved
    except OSError:
        return True


def github_clone_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


class CloneError(Exception):
    pass


class ReviewClone:
    """A single-branch clone of `url` at `path`, detached at origin/<base_branch> after each refresh.

    Who touches it when, each under `lock`: a job's Prepare for its git here (refresh, the PR head, the readiness
    threads, its job clone), a poll for its refresh and readiness, a post for its poster runs or its push of an
    address job's fix (reader()). No model session ever runs here: a job's session runs in a job clone of its own
    (make_job_clone()).
    """

    def __init__(self, path: Path, url: str, base_branch: str, token: str, timeout: float = 300.0):
        self.path = Path(path).absolute()
        self.url = url
        self._base = base_branch
        self._token = token
        self._timeout = timeout
        self.lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"ReviewClone(path={str(self.path)!r}, url={self.url!r})"

    @asynccontextmanager
    async def reader(self, *, wait: float = 600.0) -> AsyncIterator[None]:
        """A lease to run a part command from the clone while nothing moves its checkout: `lock`, waited for at
        most `wait` (a job's Prepare or a poll holds it for seconds, a post for its runs)."""
        try:
            await asyncio.wait_for(self.lock.acquire(), wait)
        except asyncio.TimeoutError:
            raise CloneError("the review clone is in use; try again in a minute") from None
        try:
            yield
        finally:
            self.lock.release()

    async def refresh(self) -> str:
        """Clone or fetch the base branch, check it out detached, and return the HEAD sha."""
        base = self._base
        self._remove_stale_locks()
        if not (self.path / ".git").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            await self._git("clone", ["clone", "--branch", base, "--single-branch", self.url, str(self.path)])
        else:
            await self._sync_origin_url()
            await self._git("fetch", ["-C", str(self.path), "fetch", "--prune", "origin",
                                      f"+refs/heads/{base}:refs/remotes/origin/{base}"])
        await self._git("checkout", ["-C", str(self.path), "-c", "advice.detachedHead=false",
                                     "checkout", "--force", "--detach", f"refs/remotes/origin/{base}"])
        await self._git("update-ref", ["-C", str(self.path), "update-ref", f"refs/heads/{base}",
                                       f"refs/remotes/origin/{base}"])
        return (await self._git("rev-parse", ["-C", str(self.path), "rev-parse", "HEAD"])).strip()

    def has_file(self, rel: str) -> bool:
        return (self.path / rel).is_file()

    def redact(self, text: str) -> str:
        """text with the clone's token masked, so callers can clean output without holding the token."""
        return redact(text, [self._token])

    async def fetch_pr_head(self, number: int) -> str:
        """Fetch the PR's head into the local branch pr-review/<number> and return its sha."""
        ref = f"refs/heads/pr-review/{number}"
        await self._git("fetch", ["-C", str(self.path), "fetch", "origin", f"+refs/pull/{number}/head:{ref}"])
        return (await self._git("rev-parse", ["-C", str(self.path), "rev-parse", ref])).strip()

    async def make_job_clone(self, dest: Path, number: int) -> ReviewClone:
        """A clone of this clone at dest for one job, with the local branches <base> and pr-review/<number>, its
        origin this clone's path (no token), not yet checked out (detach_at_base() does that, without this
        clone's lock). The caller holds `lock` and has run refresh() and fetch_pr_head(number). `--local`
        hard-links the objects, never `--shared`: the session reads only its own directories, so objects borrowed
        from this clone would be unreadable to its git. Nothing at dest is touched: a path that exists raises."""
        dest = Path(dest).absolute()
        if dest.exists() or dest.is_symlink():
            raise CloneError(f"job clone path exists: {dest}")
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        await self._git("clone", ["clone", "--local", "--no-checkout", str(self.path), str(dest)])
        job = ReviewClone(dest, str(self.path), self._base, "", self._timeout)
        await job._git("update-ref", ["-C", str(dest), "update-ref", f"refs/heads/{self._base}",
                                      f"refs/remotes/origin/{self._base}"])
        await job._git("update-ref", ["-C", str(dest), "update-ref", f"refs/heads/pr-review/{number}",
                                      f"refs/remotes/origin/pr-review/{number}"])
        return job

    async def detach_at_base(self) -> None:
        """Check the base out detached (a job clone, after make_job_clone)."""
        await self._git("checkout", ["-C", str(self.path), "-c", "advice.detachedHead=false",
                                     "checkout", "--force", "--detach", f"refs/remotes/origin/{self._base}"])

    async def delete_branch(self, number: int) -> None:
        """Remove the local branch pr-review/<number>; one that is not there is no error. A symref there is
        removed itself, never the ref it points at."""
        ref = f"refs/heads/pr-review/{number}"
        await self._git("update-ref", ["-C", str(self.path), "update-ref", "-d", "--no-deref", ref])

    async def merge_base(self, head: str) -> str:
        args = ["-C", str(self.path), "merge-base", f"refs/remotes/origin/{self._base}", head]
        return (await self._git("merge-base", args)).strip()

    async def add_worktree(self, path: Path, head: str) -> None:
        """Check head out detached at path, the PR's symlinks as plain files holding their target (a link
        is never followed out of the worktree). A worktree of this clone left there (an interrupted job's)
        is removed first; anything else at path is never touched."""
        path = Path(path).absolute()
        await self._git("worktree prune", ["-C", str(self.path), "worktree", "prune"])
        if path.exists():
            if not await self._has_worktree(path):
                raise CloneError(f"worktree path exists and is not a worktree: {path}")
            if reason := await self.remove_worktree(path):
                raise CloneError(f"worktree path in use: {path} — {reason}")
        await self._git("worktree add", ["-C", str(self.path), "-c", "core.symlinks=false",
                                         "worktree", "add", "--detach", str(path), head])

    async def remove_worktree(self, path: Path) -> str:
        """Remove the worktree at path: "" once removed or when none is registered there, else why not.
        Never --force: a worktree git refuses to remove (changes, untracked files) is left as it is."""
        path = Path(path).absolute()
        try:
            if await self._has_worktree(path):
                # As it was checked out: with symlinks on, git would see each plain-file link as a change.
                await self._git("worktree remove", ["-C", str(self.path), "-c", "core.symlinks=false",
                                                    "worktree", "remove", str(path)])
        except CloneError as e:
            return str(e)
        return ""

    async def push_patch(self, *, number: int, head: str, branch: str, patch: Path, message: str,
                         author: tuple[str, str], worktree: Path) -> str:
        """Commit `patch` on the PR's head in a worktree at `worktree` and push it to `branch`; the new sha.
        The caller holds `lock`. The push lands only while the branch is exactly at `head`: a branch that moved,
        forward or back, makes it fail, and nothing on the branch is ever replaced. The worktree is left at
        `worktree`: the caller removes its directory."""
        if await self.fetch_pr_head(number) != head:
            raise CloneError("head moved")
        await self.add_worktree(worktree, head)
        wt = ["-C", str(worktree), "-c", "core.symlinks=false"]
        await self._git("apply", [*wt, "apply", "--index", "--binary", str(patch)])
        name, email = author
        await self._git("commit", [*wt, "-c", f"user.name={name}", "-c", f"user.email={email}",
                                   "commit", "--no-verify", "-m", message])
        sha = (await self._git("rev-parse", ["-C", str(worktree), "rev-parse", "HEAD"])).strip()
        if not _FULL_SHA.fullmatch(sha):
            raise CloneError(f"the new commit is not a full id: {sha[:40]!r}")
        # The peeled id, never a name: git matches a push source against ref names before object ids. No + and no
        # --force. The lease names the head it expects, so it only ever refuses more than a plain push: the new
        # commit is that head's child, a fast-forward from it, and a branch set back to an ancestor of the head
        # (which a plain push would fast-forward too) is refused as one that moved on is.
        await self._git("push", ["-C", str(self.path), "push", "--no-signed", "--recurse-submodules=no",
                                 f"--force-with-lease=refs/heads/{branch}:{head}", "origin",
                                 f"{sha}^{{commit}}:refs/heads/{branch}"])
        return sha

    async def _has_worktree(self, path: Path) -> bool:
        out = await self._git("worktree list", ["-C", str(self.path), "worktree", "list", "--porcelain"])
        # git lists real paths; path may reach the same directory through a symlink (/var vs /private/var).
        listed = {Path(line.removeprefix("worktree ")).resolve()
                  for line in out.splitlines() if line.startswith("worktree ")}
        return path.resolve() in listed

    async def _sync_origin_url(self) -> None:
        """Point origin at self.url: the clone keeps the URL it was made with, and the repo's
        owner/repo may have changed since. A missing origin reads as "", so set-url says why."""
        args = ["-C", str(self.path), "config", "--get", "--default", "", "remote.origin.url"]
        if (await self._git("config", args)).strip() != self.url:
            await self._git("remote", ["-C", str(self.path), "remote", "set-url", "origin", self.url])

    def _remove_stale_locks(self) -> None:
        # Cleave owns the clone and serializes every git call on it, so a lock seen here is a killed git's.
        # Only where git keeps its locks: .git itself and refs/. Never objects/ (large) or worktrees/.
        git_dir = self.path / ".git"
        for lock in [*git_dir.glob("*.lock"), *(git_dir / "refs").glob("**/*.lock")]:
            lock.unlink()

    async def _git(self, verb: str, args: list[str]) -> str:
        # path.parent always exists here: the clone branch creates it, and path/.git implies it.
        # The ceiling keeps `git -C path` from walking up into an enclosing repo when path/.git is broken.
        env = {**git_env(self._token), "GIT_CEILING_DIRECTORIES": str(self.path.parent)}
        result = await run_command(["git", *args], cwd=self.path.parent, env=env, timeout=self._timeout)
        if result.timed_out:
            raise CloneError(f"git {verb} timed out after {int(self._timeout)} s")
        if result.returncode != 0:
            # Redact before cutting, so a token straddling the cut cannot leak in part.
            raise CloneError(f"git {verb} failed: {stderr_tail(redact(result.stderr, [self._token]))}")
        return result.stdout
