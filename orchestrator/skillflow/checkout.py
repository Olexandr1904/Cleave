"""The skill checkout: the operator's long-lived clone of the target repo, of which every skill run gets a checkout
of its own (a worktree) in which the skill session makes the ticket's worktree; and every git call Cleave itself
makes there and in its courier."""
from __future__ import annotations

import asyncio
import re
import shutil
from contextlib import suppress
from enum import Enum
from itertools import islice, takewhile
from pathlib import Path
from urllib.parse import urlsplit

from integrations.base.ticket_key import carries_key
from orchestrator.reviewer.clone import git_env
from orchestrator.reviewer.commands import NO_HOST_GIT_ENV, base_env, redact, run_command, stderr_tail

_REASON_LIMIT = 300
_LISTED = 3   # the file names kept from a list git prints after a reason
_ORIGIN_REFS = "+refs/remotes/origin/*:refs/remotes/origin/*"   # between the checkout and the courier
_COURIER_MARK = "cleave-courier"   # in every courier Cleave made; nothing else at that path is used or removed
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
# Settings that make git start a program or reach into another repository, held off on the calls that carry the
# token (GIT_ALLOW_PROTOCOL holds those to the URL's transport). Those calls read only the courier's config,
# which Cleave wrote, so most of these are a second line.
_NETWORK_OVERRIDES = (
    # Not a second line: this alone keeps git from running for-each-ref in the courier's alternate, the
    # checkout, whose config is the session's, with the token in its environment.
    ("core.alternateRefsCommand", "true"),
    ("push.gpgSign", "false"),               # a signed push runs gpg.program
    ("fetch.recurseSubmodules", "false"),    # a submodule's fetch or push reads that submodule's config
    ("push.recurseSubmodules", "no"),
    ("submodule.recurse", "false"),
)


class CheckoutError(Exception):
    pass


class _Courier(Enum):
    """Where a git call in the courier goes: between the courier and the checkout, or to the forge (the only
    calls that hold the token)."""
    LOCAL = "local"
    FORGE = "forge"


def _local_env() -> dict[str, str]:
    """The environment of a git call that stays on this host: no token, no host config, no prompt, no hook or
    fsmonitor (environment config outranks .git/config), and no gc or maintenance still running after the call
    returns."""
    return {
        **base_env(),
        **NO_HOST_GIT_ENV,
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "core.fsmonitor",
        "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "gc.autoDetach",
        "GIT_CONFIG_VALUE_2": "false",
        "GIT_CONFIG_KEY_3": "maintenance.autoDetach",
        "GIT_CONFIG_VALUE_3": "false",
    }


def _network_env(token: str, protocol: str) -> dict[str, str]:
    """git_env(token), held to the transport of the checkout's own URL and with _NETWORK_OVERRIDES on top."""
    env = git_env(token)
    first = int(env["GIT_CONFIG_COUNT"])
    for i, (key, value) in enumerate(_NETWORK_OVERRIDES, start=first):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    env["GIT_CONFIG_COUNT"] = str(first + len(_NETWORK_OVERRIDES))
    # An insteadOf cannot send the call through ssh's or ext's program: this outranks every protocol.* setting.
    env["GIT_ALLOW_PROTOCOL"] = protocol
    return env


def _reason(stderr: str) -> str:
    """The line of git's stderr that says why (the first error, fatal or rejected line, with up to three of the
    file names git lists under it), else its last line; cut to _REASON_LIMIT characters."""
    lines = stderr.splitlines()
    for i, line in enumerate(lines):
        text = line.strip()
        if text.startswith(("error:", "fatal:")) or "[rejected]" in text or "! [remote rejected]" in text:
            if text.endswith(":"):
                listed = takewhile(lambda after: after[:1].isspace() and after.strip(), lines[i + 1:])
                if names := [name.strip() for name in islice(listed, _LISTED)]:
                    text = f"{text} {', '.join(names)}"
            return text[:_REASON_LIMIT]
    return stderr_tail(stderr, _REASON_LIMIT)


class SkillCheckout:
    """The clone at `path`, made once by hand. Cleave fetches it before each run and never moves it: each run
    works in a checkout of its own, a worktree of this one at origin/<base_branch> (detached), which shares its
    objects, branches and config. Cleave never forces anything: a checkout, a push or a worktree removal git
    refuses is reported, not overridden. The calls that change what every run shares (the fetch, the courier,
    the push, a worktree made or removed) and the reads of the fetched branches take turns (one lock), as runs
    of the repo work at once.

    The session can write the checkout's .git/config, and git runs the programs it names (hooks, drivers,
    ssh), so the forge token never meets that file. The fetch and the push run in the courier, a bare
    repository next to the checkout whose config Cleave wrote and which borrows the checkout's objects through
    alternates; token-less local calls move refs between the two. The token lives only in those two calls'
    environment, never in a config file, and the origin URL in .git/config is left as the operator set it."""

    def __init__(self, path: Path, url: str, base_branch: str, token: str, timeout: float = 300.0):
        self.path = Path(path).absolute()
        self.base_branch = base_branch
        self._url = url
        self._token = token
        self._timeout = timeout
        self._courier = self.path.parent / (self.path.name + ".courier.git")
        self._lock = asyncio.Lock()

    async def prepare(self) -> str:
        """Fetch every branch and return origin/<base>'s sha. The checkout itself stays where it is."""
        if not (self.path / ".git").exists():
            raise CheckoutError(f"the skill checkout is missing at {self.path}: "
                                "clone it by hand (deploy/README.md)")
        # The courier borrows the checkout's objects, and a shallow store cannot be borrowed without its
        # shallow file.
        if (self.path / ".git" / "shallow").exists():
            raise CheckoutError("the skill checkout is a shallow clone: clone it in full (deploy/README.md)")
        async with self._lock:
            # Made anew: refs left from an earlier run can clash with the checkout's or name objects it no
            # longer has, and would stop this fetch every time.
            await self._courier_ready()
            # The courier starts from the checkout's branches, so the fetch asks the forge only for what is new.
            # Not its tags: those would travel back, and a tag deleted in the checkout would return.
            await self._git("fetch", ["fetch", "--prune", "--no-tags", str(self.path), _ORIGIN_REFS],
                            _Courier.LOCAL)
            await self._git("fetch", ["fetch", "--prune", self._url, "+refs/heads/*:refs/remotes/origin/*"],
                            _Courier.FORGE)
            # A submodule's own fetch would run here without the token, and read that submodule's config.
            await self._git("fetch", ["fetch", "--prune", "--no-recurse-submodules", str(self._courier),
                                      _ORIGIN_REFS], self.path)
            return (await self._git("rev-parse", ["rev-parse", f"refs/remotes/origin/{self.base_branch}"],
                                    self.path)).strip()

    async def make_run_checkout(self, dest: Path) -> None:
        """Make dest the run's own checkout: a worktree of this checkout, detached at origin/<base>. One made
        there before (a prepare Approve runs again) is moved to origin/<base>, never forced: untracked files
        stay, local changes the new base would overwrite stop it. Anything else at dest stops it, untouched."""
        dest = Path(dest).absolute()
        base = f"refs/remotes/origin/{self.base_branch}"
        async with self._lock:
            if await self._has_worktree(dest):
                try:
                    await self._git("checkout", ["-c", "advice.detachedHead=false", "checkout", "--detach", base],
                                    dest)
                except CheckoutError as e:
                    raise CheckoutError(f"the run's checkout could not move to origin/{self.base_branch}: {e}") \
                        from None
                return
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                await self._git("worktree add", ["worktree", "add", "--detach", str(dest), base], self.path)
            except (CheckoutError, OSError) as e:
                raise CheckoutError(f"the run's checkout could not be made at {dest}: {e}"[:_REASON_LIMIT]) \
                    from None

    def missing_files(self, required: list[str]) -> list[str]:
        return [rel for rel in required if not (self.path / rel).exists()]

    def copy_files(self, root: Path, required: list[str]) -> None:
        """Copy each of the required files from this checkout to the same place in the checkout at root,
        replacing an earlier copy; never through a symlink there."""
        for rel in required:
            source = self.path / rel
            if not source.is_file():
                raise CheckoutError(f"required file {rel} in the skill checkout is not a file")
            if (root / rel).is_symlink():
                raise CheckoutError(f"{rel} in the run's checkout is a symlink: no file is copied through one")
            dest = self._inside(root, rel)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
            except OSError as e:
                raise CheckoutError(f"{rel} could not be copied to the run's checkout: {e}"[:_REASON_LIMIT]) \
                    from None

    def write_ticket(self, root: Path, source: Path, notes_rel: str) -> Path:
        """Copy the ticket's files (source/*) into <root>/<notes_rel>/ticket and return that directory. An
        earlier copy there is removed first, so no stale file stays; the rest of the notes directory is the
        session's."""
        dest = self._inside(root, notes_rel) / "ticket"
        try:
            if dest.is_symlink():
                raise CheckoutError(f"{notes_rel}/ticket in the run's checkout is a symlink: no ticket is "
                                    "written through one")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(source, dest)
        except OSError as e:
            raise CheckoutError(f"the ticket could not be written to {notes_rel}/ticket: {e}"[:_REASON_LIMIT]) \
                from None
        return dest

    async def branches_with(self, key: str) -> list[str]:
        """The fetched branches that hold the key as a whole key (carries_key), as open PRs are matched."""
        async with self._lock:
            out = await self._git("for-each-ref", ["for-each-ref", "--format=%(refname:lstrip=3)",
                                                   "refs/remotes/origin/"], self.path)
        return [name for name in out.splitlines() if name != "HEAD" and carries_key(name, key)]

    async def on_forge(self, branch: str) -> bool:
        """Whether the branch was on the forge at the last fetch: refs/remotes/origin/<branch> in the checkout."""
        ref = f"refs/remotes/origin/{branch}"
        # A pattern also matches the refs below it (origin/release matches origin/release/2.0): only the whole name.
        # Not read while another run's fetch moves the refs.
        async with self._lock:
            out = await self._git("for-each-ref", ["for-each-ref", "--format=%(refname)", ref], self.path)
        return ref in out.splitlines()

    def worktree(self, root: Path, rel: str) -> Path:
        """The ticket's worktree, rel inside the checkout at root (the run's own)."""
        return self._inside(root, rel)

    async def has_change(self, worktree: Path) -> bool:
        """Whether the worktree exists and holds anything new: a change not committed, or a commit past the
        base."""
        if not worktree.is_dir():
            return False
        if (await self._git("status", ["status", "--porcelain"], worktree)).strip():
            return True
        return await self._commits_ahead(worktree) > 0

    async def commit_state(self, worktree: Path) -> tuple[str, str]:
        """(branch, problem): what keeps the worktree's change from being pushed as it is; "" when nothing."""
        # Not `symbolic-ref -q`: it exits 1 on a detached HEAD, which _git reads as a failure.
        branch = (await self._git("branch", ["branch", "--show-current"], worktree)).strip()
        if not branch:
            return "", "not on a branch"
        out = await self._git("status", ["status", "--porcelain"], worktree)
        status = [line.strip() for line in out.splitlines() if line.strip()]
        if status:
            return branch, f"not committed: {'; '.join(status[:3])}"
        if await self._commits_ahead(worktree) == 0:
            return branch, "no commit on the branch"
        return branch, ""

    async def head(self, worktree: Path) -> str:
        """The sha of the worktree's HEAD."""
        return (await self._git("rev-parse", ["rev-parse", "HEAD"], worktree)).strip()

    async def changed_files(self, worktree: Path) -> list[str]:
        """The files the branch changed since it left origin/<base> (merge-base .. HEAD)."""
        out = await self._git("diff", ["diff", "--name-only", "-z",
                                       f"refs/remotes/origin/{self.base_branch}...HEAD"], worktree)
        return [rel for rel in out.split("\0") if rel]

    async def push(self, branch: str, sha: str) -> None:
        """Push the commit `sha` to the branch of the same name; a remote that moved on rejects it. The commit, not
        the branch by name: one made on the branch since it was judged stays here. It reaches the forge from the
        courier, staged there as refs/skill/<branch> for the push only. The base branch is never pushed: refused
        before any git runs; a commit off the branch's history before anything reaches the forge."""
        if branch == self.base_branch:
            raise CheckoutError(f"{branch} is the base branch, which Cleave never pushes")
        # A full id only: a short one, HEAD or a branch name could name another commit by the time git reads it.
        if not _FULL_SHA.fullmatch(sha):
            raise CheckoutError(f"{sha!r} is not a full commit id")
        # Peeled: git matches a push source against local ref names before it reads it as an object id, and the
        # session can make a branch or a tag named as the sha on another commit.
        commit = f"{sha}^{{commit}}"
        try:
            await self._git("merge-base", ["merge-base", "--is-ancestor", commit, f"refs/heads/{branch}"],
                            self.path)
        except CheckoutError:
            raise CheckoutError(f"the commit {sha} is not on {branch}") from None
        staged = f"refs/skill/{branch}"
        try:
            async with self._lock:
                # Made anew as well: a ref seeded at prepare time can dangle by now (the branch deleted on the
                # forge, then a prune and a gc in the checkout), and would stop this push every time. So no staging
                # ref is left from an earlier push either: a rewritten branch reaches the forge, to be refused
                # there.
                await self._courier_ready()
                # The courier takes no signed push, so a push.gpgSign the checkout holds would stop this one; and
                # the checkout's submodules are not the branch's to push.
                await self._git("push", ["push", "--no-signed", "--recurse-submodules=no", str(self._courier),
                                         f"{commit}:{staged}"], self.path)
                try:
                    await self._git("push", ["push", self._url, f"{staged}:refs/heads/{branch}"], _Courier.FORGE)
                finally:
                    # The staging ref is for this push only. Best effort: the push's own result or error is what
                    # counts.
                    with suppress(CheckoutError):
                        await self._git("update-ref", ["update-ref", "-d", staged], _Courier.LOCAL)
        except CheckoutError as e:
            raise CheckoutError(f"push rejected: {e}") from None

    async def remove_worktree(self, worktree: Path) -> str:
        """Remove the worktree (the ticket's, or a run's checkout): "" once removed or when none is registered
        there, else why not. A worktree git refuses to remove (changes, untracked files) is left as it is."""
        try:
            async with self._lock:
                if await self._has_worktree(worktree):
                    await self._git("worktree remove", ["worktree", "remove", str(worktree)], self.path)
        except CheckoutError as e:
            return str(e)
        except (RuntimeError, OSError) as e:   # a symlink loop in the path
            return f"the worktree path cannot be resolved: {e}"[:_REASON_LIMIT]
        return ""

    def redact(self, text: str) -> str:
        """text with the token masked, so callers can clean output without holding the token."""
        return redact(text, [self._token])

    def _inside(self, root: Path, rel: str) -> Path:
        """<root>/<rel>, which must lead inside the checkout at root: not absolute, not out through `..` or a
        symlink."""
        try:
            target, resolved = (root / rel).resolve(), root.resolve()
        except (RuntimeError, OSError) as e:   # a symlink loop: RuntimeError up to Python 3.12, OSError after
            raise CheckoutError(f"{rel!r} in the checkout at {root} cannot be resolved: {e}"[:_REASON_LIMIT]) \
                from None
        if target == resolved or not target.is_relative_to(resolved):
            raise CheckoutError(f"{rel!r} is not inside the checkout at {root}")
        return root / rel

    async def _commits_ahead(self, worktree: Path) -> int:
        args = ["rev-list", "--count", f"refs/remotes/origin/{self.base_branch}..HEAD"]
        return int((await self._git("rev-list", args, worktree)).strip())

    async def _has_worktree(self, worktree: Path) -> bool:
        """Whether a worktree is registered at the path. One whose directory is gone (removed by hand) is pruned
        and reads as none: git would neither move nor remove it."""
        target = Path(worktree).resolve()   # first: a symlink loop in the path raises, as it did before
        if not target.exists():
            await self._git("worktree prune", ["worktree", "prune"], self.path)
            return False
        out = await self._git("worktree list", ["worktree", "list", "--porcelain"], self.path)
        # git lists real paths; worktree may reach the same directory through a symlink (/var vs /private/var).
        listed = {Path(line.removeprefix("worktree ")).resolve()
                  for line in out.splitlines() if line.startswith("worktree ")}
        return target in listed

    async def _courier_ready(self) -> None:
        """Make the courier anew, pointing at the checkout's objects. Only a directory holding _COURIER_MARK, or
        an empty one (made, then stopped before the mark), is removed; the mark goes last, so a removal stopped
        part-way leaves a directory that is still Cleave's to remove."""
        courier = self._courier
        try:
            if courier.is_symlink() or courier.exists():
                ours = not courier.is_symlink() and courier.is_dir() and (
                    (courier / _COURIER_MARK).is_file() or not any(courier.iterdir()))
                if not ours:
                    raise CheckoutError(f"Cleave did not make {courier}, where the skill checkout's courier goes: "
                                        "remove it by hand")
                for entry in courier.iterdir():
                    if entry.name == _COURIER_MARK:
                        continue
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                (courier / _COURIER_MARK).unlink(missing_ok=True)
                courier.rmdir()
            # Marked before git writes anything, so a courier whose making failed is still Cleave's to remove.
            courier.mkdir()
            (courier / _COURIER_MARK).write_text("Cleave's courier: Cleave removes and remakes it.\n")
            await self._git("init", ["init", "--bare"], _Courier.LOCAL)
            (courier / "objects" / "info" / "alternates").write_text(f"{self.path / '.git' / 'objects'}\n")
        except OSError as e:
            raise CheckoutError(f"the courier at {courier} cannot be used: {e}"[:_REASON_LIMIT]) from None

    async def _git(self, verb: str, args: list[str], repo: Path | _Courier) -> str:
        """git in the checkout or one of its worktrees (a Path), or in the courier, named as the git directory
        so nothing is looked up; only _Courier.FORGE holds the token."""
        env = _network_env(self._token, urlsplit(self._url).scheme) if repo is _Courier.FORGE else _local_env()
        env["LC_ALL"] = "C"   # _reason reads git's own words, and the reason reaches the operator in English
        if isinstance(repo, _Courier):
            where = ["--git-dir", str(self._courier)]
        else:
            cwd = Path(repo).absolute()
            where = ["-C", str(cwd)]
            # The ceiling keeps git from walking up out of a broken checkout or worktree into the repo around
            # it (a worktree's is the checkout itself). The process runs from the checkout's parent, so a
            # worktree that is not there reads as git's own "cannot change to" error.
            env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
        result = await run_command(["git", *where, *args], cwd=self.path.parent, env=env, timeout=self._timeout)
        if result.timed_out:
            raise CheckoutError(f"git {verb} timed out after {int(self._timeout)} s")
        if result.returncode != 0:
            # Redact before cutting, so a token straddling the cut cannot leak in part.
            raise CheckoutError(f"git {verb} failed: {_reason(self.redact(result.stderr))}")
        return result.stdout
