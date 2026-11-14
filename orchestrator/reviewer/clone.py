"""The review clone: the daemon's own checkout of a reviewed repo, kept at the base branch."""
from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from orchestrator.reviewer.commands import NO_HOST_GIT_ENV, base_env, redact, run_command, stderr_tail

# Where a setting planted in the clone makes git run a program: .git/config (helpers, filter, diff and merge
# drivers) and .git/info/attributes (the files a driver applies to). A job saves them before its session.
_GUARDED_GIT_FILES = ("config", "info/attributes")


def git_env(token: str) -> dict[str, str]:
    """base_env() plus git config that resets every credential helper and sends the token as a header.

    The token lives only in this environment: never in .git/config, never in the keychain. System and
    global config are not read at all, so no askPass, helper or insteadOf from the host can apply.
    Auto maintenance stays on but runs in the foreground, so nothing touches .git after a call returns.
    Hooks and fsmonitor are off (environment config outranks .git/config); the rest of .git/config is the
    clone's own, which a job writes back after its session.
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

    Who touches it when: a review job holds `lock` for its whole run, Prepare through cleanup; a poll holds it
    for its refresh and readiness while no job does. In a job's session phase (session_phase()) the job runs no
    git in the clone and its checkout stays at the base the job prepared from: a poll then runs readiness, with
    no refresh, under `session_lock`, which the job also takes to enter and to leave that phase. While a job
    prepares or cleans up, a poll leaves the clone alone.
    """

    def __init__(self, path: Path, url: str, base_branch: str, token: str, timeout: float = 300.0):
        self.path = Path(path).absolute()
        self.url = url
        self._base = base_branch
        self._token = token
        self._timeout = timeout
        self.lock = asyncio.Lock()
        self.session_lock = asyncio.Lock()
        # Both changed only under session_lock.
        self.in_session = False
        self._saved: dict[Path, bytes | None] = {}

    def __repr__(self) -> str:
        return f"ReviewClone(path={str(self.path)!r}, url={self.url!r})"

    @asynccontextmanager
    async def session_phase(self, saved: dict[Path, bytes | None]) -> AsyncIterator[None]:
        """A job's model session; the job holds `lock`. saved: guarded_git_files() before the session, which a
        poll compares with before its readiness. Leaving waits for a poll's readiness in flight, so the job's
        cleanup never overlaps it."""
        async with self.session_lock:
            self.in_session, self._saved = True, saved
        try:
            yield
        finally:
            try:
                await self.session_lock.acquire()
            finally:  # cancelled while it waits: out of the phase all the same
                self.in_session, self._saved = False, {}
            self.session_lock.release()

    def changed_git_files(self) -> list[Path]:
        """In a job's session phase: the guarded git files that no longer hold what the job saved."""
        return [path for path, saved in self._saved.items() if git_file_changed(path, saved)]

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
