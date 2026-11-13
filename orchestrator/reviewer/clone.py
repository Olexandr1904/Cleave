"""The review clone: the daemon's own checkout of a reviewed repo, kept at the base branch."""
from __future__ import annotations

import base64
from pathlib import Path

from orchestrator.reviewer.commands import base_env, redact, run_command, stderr_tail


def git_env(token: str) -> dict[str, str]:
    """base_env() plus git config that resets every credential helper and sends the token as a header.

    The token lives only in this environment: never in .git/config, never in the keychain. System and
    global config are not read at all, so no askPass, helper or insteadOf from the host can apply.
    Auto maintenance stays on but runs in the foreground, so nothing touches .git after a call returns.
    """
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **base_env(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_1": f"AUTHORIZATION: basic {header}",
        "GIT_CONFIG_KEY_2": "maintenance.autoDetach",
        "GIT_CONFIG_VALUE_2": "false",
        "GIT_CONFIG_KEY_3": "gc.autoDetach",
        "GIT_CONFIG_VALUE_3": "false",
    }


def github_clone_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


class CloneError(Exception):
    pass


class ReviewClone:
    """A single-branch clone of `url` at `path`, detached at origin/<base_branch> after each refresh."""

    def __init__(self, path: Path, url: str, base_branch: str, token: str, timeout: float = 300.0):
        self.path = Path(path).absolute()
        self.url = url
        self._base = base_branch
        self._token = token
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"ReviewClone(path={str(self.path)!r}, url={self.url!r})"

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
