"""Credential health: whether every token and login Cleave depends on still works.

Checks each credential in the background, on demand, and again when an adapter
reports a 401/403. Only the environment variable's name is kept or shown, never
a token value: the token lives solely in each credential's `check` closure.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Awaitable, Callable

from config.schemas import GlobalConfig, LoadedProject, ProjectConfig, RepoConfig
from health.validators import (
    ValidatorResult,
    check_claude_login,
    check_github,
    check_gitlab,
    check_jira,
    check_telegram,
    check_trello,
)
from integrations.telegram.resolve import daemon_bot_token
from orchestrator.tg_i18n import ENGLISH, N_, telegram_language, tr

logger = logging.getLogger(__name__)

LOGIN_EXPIRING = timedelta(days=3)
LOGIN_LAST_DAY = timedelta(days=1)
_FAILING = frozenset({"invalid", "no_access"})
_RELOGIN_FIX = N_("Log in again on the host in a GUI terminal.")


@dataclass
class Credential:
    """One credential to check; `check` runs its validator with the token."""
    key: str
    kind: str
    provider: str
    label: str
    target: str
    env_var: str
    field: str
    check: Callable[[], Awaitable[ValidatorResult]]


@dataclass
class CredentialStatus:
    key: str
    kind: str
    provider: str
    label: str
    target: str
    env_var: str
    field: str
    state: str = "unknown"
    reason: str = ""
    fix: str = ""
    http_status: int | None = None
    since: datetime | None = None
    checked_at: datetime | None = None
    expires_at: datetime | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        for name in ("since", "checked_at", "expires_at"):
            data[name] = data[name].isoformat() if data[name] else None
        return data


def build_credentials(
    global_config: GlobalConfig, projects: dict[str, LoadedProject], claude_bin: str,
) -> list[Credential]:
    """Every configured credential, in order: per project its tracker, then its repos' forges;
    then the Telegram bot; then the claude login."""
    creds: list[Credential] = []
    for project_id, project in projects.items():
        tracker = _tracker_credential(project_id, project.config)
        if tracker is not None:
            creds.append(tracker)
        for repo_id, repo in project.repos.items():
            forge = _forge_credential(f"forge:{project_id}/{repo_id}", repo)
            if forge is not None:
                creds.append(forge)
    telegram = _telegram_credential(global_config, projects)
    if telegram is not None:
        creds.append(telegram)
    creds.append(Credential(
        "claude_login", "claude_login", "claude", N_("claude login"), "host", "", "",
        lambda: check_claude_login(claude_bin),
    ))
    return creds


def _tracker_credential(project_id: str, config: ProjectConfig) -> Credential | None:
    tracker, refs, key = config.tracker, config.env_refs, f"tracker:{project_id}"
    if tracker.provider == "jira" and tracker.jira.url and tracker.jira.token:
        jira = tracker.jira
        # A legacy top-level `jira:` block records its ref as `jira.token`.
        env_var = refs.get("tracker.jira.token") or refs.get("jira.token", "")
        return Credential(
            key, "tracker", "jira", f"Jira {jira.project_key}", jira.project_key,
            env_var, "tracker.jira.token",
            lambda: check_jira(jira.url, jira.email, jira.token, jira.project_key),
        )
    if tracker.provider == "trello" and tracker.trello.api_key and tracker.trello.token:
        trello = tracker.trello
        return Credential(
            key, "tracker", "trello", f"Trello {trello.board_id}", trello.board_id,
            refs.get("tracker.trello.token", ""), "tracker.trello.token",
            lambda: check_trello(trello.api_key, trello.token, trello.board_id),
        )
    return None


def _forge_credential(key: str, repo: RepoConfig) -> Credential | None:
    vcs, refs = repo.vcs, repo.env_refs
    if vcs.provider == "github" and vcs.github.token:
        github = vcs.github
        target = f"{github.owner}/{github.repo}"
        return Credential(
            key, "forge", "github", f"GitHub {target}", target,
            refs.get("vcs.github.token", ""), "vcs.github.token",
            lambda: check_github(github.token, github.owner, github.repo),
        )
    if vcs.provider == "gitlab" and vcs.gitlab.token:
        gitlab = vcs.gitlab
        return Credential(
            key, "forge", "gitlab", f"GitLab {gitlab.project_id}", gitlab.project_id,
            refs.get("vcs.gitlab.token", ""), "vcs.gitlab.token",
            lambda: check_gitlab(
                gitlab.token, gitlab.project_id, gitlab.url or "https://gitlab.com",
            ),
        )
    return None


def _telegram_credential(
    global_config: GlobalConfig, projects: dict[str, LoadedProject],
) -> Credential | None:
    """The token the daemon's notifier uses."""
    token, env_var = daemon_bot_token(global_config, projects)
    if not token:
        return None
    return Credential(
        "telegram", "telegram", "telegram", "Telegram bot", "bot",
        env_var, "telegram.bot_token",
        lambda: check_telegram(token),
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    if value.endswith("Z"):  # Python 3.10's fromisoformat rejects the Z suffix
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def classify(
    cred: Credential, result: ValidatorResult, now: datetime,
) -> tuple[str, datetime | None]:
    """The credential's state for this result, and the login's expiry when known."""
    if cred.kind == "claude_login":
        logged_in = result.extra.get("logged_in")
        if logged_in is None:
            return "unreachable", None
        if not logged_in:
            return "invalid", None
        expires_at = _parse_time(result.extra.get("expires_at"))
        if expires_at is None:
            return "ok", None
        if expires_at <= now:
            return "invalid", expires_at
        if expires_at <= now + LOGIN_EXPIRING:
            return "expiring", expires_at
        return "ok", expires_at
    if result.ok:
        return "ok", None
    status = result.extra.get("status")
    if status == 401 or (cred.kind == "telegram" and status == 404):
        return "invalid", None
    if status in (403, 404):
        return "no_access", None
    return "unreachable", None


def fix_text(cred: Credential, state: str, reason: str = "") -> str:
    """The fix, in the current Telegram language: English for the dashboard's status, the line's language for a
    Telegram line (CredentialHealth makes each in its own)."""
    if state == "unreachable":
        if cred.kind == "claude_login" and reason == "claude not found":
            return tr("Add the directory with claude (/opt/homebrew/bin) to the daemon's PATH and restart it.")
        return tr("Network or service unavailable; next check in 15 min.")
    if cred.kind == "claude_login":
        return tr(_RELOGIN_FIX) if state in ("invalid", "expiring") else ""
    if state == "invalid":
        if cred.env_var:
            return tr("Create a new token, put it in .env as {env_var}, restart the daemon.", env_var=cred.env_var)
        return tr("Create a new token, set it in {field}, restart the daemon.", field=cred.field)
    if state == "no_access":
        if cred.provider == "github":
            return tr("Check the token's access to {target}; "
                      "a classic GitHub token may need SSO authorization for the organization.", target=cred.target)
        return tr("Check the token's access to {target}.", target=cred.target)
    return ""


def _unknown_status(cred: Credential) -> CredentialStatus:
    return CredentialStatus(
        cred.key, cred.kind, cred.provider, cred.label, cred.target, cred.env_var, cred.field,
    )


@dataclass
class _Memory:
    """What one credential's Telegram lines depend on."""
    status: CredentialStatus
    previous: str = "ok"                  # last state not unknown/unreachable; none yet = ok
    warned_for: datetime | None = None    # the login expiry the warnings below refer to
    warned_days: int | None = None        # closest expiry warning sent for it: 3 or 1


class CredentialHealth:
    """Holds the latest state of every credential and tells Telegram when one breaks or recovers."""

    def __init__(
        self,
        credentials_fn: Callable[[], list[Credential]],
        send_line: Callable[[str], Awaitable[Any]] | None = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        interval_seconds: float = 900.0,
        recheck_gap_seconds: float = 60.0,
        tz: tzinfo | None = None,
        language: Callable[[], str] = lambda: ENGLISH,
    ) -> None:
        self._credentials_fn = credentials_fn
        self._send_line = send_line
        self._language = language  # the Telegram lines' (global.yaml's telegram.language)
        self._now = now
        self._interval = interval_seconds
        self._recheck_gap = timedelta(seconds=recheck_gap_seconds)
        self._tz = tz
        self._memory: dict[str, _Memory] = {}
        self._inflight: dict[str, asyncio.Task[CredentialStatus]] = {}
        # Set once run()'s first round has returned or raised: the reviewer's parts wait for it.
        self.first_round = asyncio.Event()

    async def check_all(self) -> list[CredentialStatus]:
        creds = self._credentials_fn()
        keys = {cred.key for cred in creds}
        self._memory = {key: memory for key, memory in self._memory.items() if key in keys}
        # Shielded: a cancelled caller must not cancel a check other callers share.
        return list(await asyncio.gather(*(asyncio.shield(self._start(cred)) for cred in creds)))

    async def check_one(self, key: str) -> CredentialStatus | None:
        cred = self._find(key)
        return None if cred is None else await asyncio.shield(self._start(cred))

    def report_auth_failure(self, key: str, status: int) -> None:
        """An adapter got a 401/403: re-check this credential soon. Never raises."""
        try:
            if key in self._inflight:
                return
            memory = self._memory.get(key)
            if memory is not None and self._now() - memory.status.checked_at < self._recheck_gap:
                return
            cred = self._find(key)
            if cred is None:
                return
            self._start(cred)
            logger.info("Credential %s got HTTP %s; re-checking", key, status)
        except Exception as e:
            logger.debug("Credential re-check for %s not started: %s", key, type(e).__name__)

    def snapshot(self) -> list[CredentialStatus]:
        statuses = []
        for cred in self._credentials_fn():
            memory = self._memory.get(cred.key)
            statuses.append(memory.status if memory is not None else _unknown_status(cred))
        return statuses

    def state(self, key: str) -> str:
        memory = self._memory.get(key)
        return memory.status.state if memory is not None else "unknown"

    def failing(self, key: str) -> bool:
        return self.state(key) in _FAILING

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.check_all()
            except Exception:
                logger.exception("Credential health round failed")
            self.first_round.set()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    def _find(self, key: str) -> Credential | None:
        return next((cred for cred in self._credentials_fn() if cred.key == key), None)

    def _start(self, cred: Credential) -> asyncio.Task[CredentialStatus]:
        """The check of this key already in flight, else a new one: never two at once."""
        task = self._inflight.get(cred.key)
        if task is None:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._check(cred))
            self._inflight[cred.key] = task
            task.add_done_callback(lambda done, key=cred.key: self._forget(key, done))
        return task

    def _forget(self, key: str, task: asyncio.Task[CredentialStatus]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]

    async def _check(self, cred: Credential) -> CredentialStatus:
        try:
            result = await cred.check()
        except Exception as e:
            result = ValidatorResult(
                False, cred.kind, cred.target, type(e).__name__, "", extra={"status": None},
            )
        return await self._apply(cred, result)

    async def _apply(self, cred: Credential, result: ValidatorResult) -> CredentialStatus:
        now = self._now()
        state, expires_at = classify(cred, result, now)
        memory = self._memory.get(cred.key)
        since = memory.status.since if memory is not None and memory.status.state == state else now
        with telegram_language(ENGLISH):  # the dashboard's, whichever task asked for the check
            fix = fix_text(cred, state, result.reason)
        status = CredentialStatus(
            cred.key, cred.kind, cred.provider, cred.label, cred.target, cred.env_var, cred.field,
            state=state, reason=result.reason, fix=fix,
            http_status=result.extra.get("status"),
            since=since, checked_at=now, expires_at=expires_at,
        )
        if memory is None:
            memory = self._memory[cred.key] = _Memory(status)
        memory.status = status
        with telegram_language(self._language()):
            for line in self._lines(cred, status, memory, now):
                await self._send(line)
        return status

    def _lines(
        self, cred: Credential, status: CredentialStatus, memory: _Memory, now: datetime,
    ) -> list[str]:
        """Telegram lines for this result, in the current language; updates the memory they depend on."""
        previous = memory.previous
        if status.state not in ("unknown", "unreachable"):
            memory.previous = status.state
        if cred.kind == "telegram":
            return []
        lines = []
        label = tr(cred.label)  # a name ("GitHub acme/app"), or the login's, which the catalogue holds
        if status.state in _FAILING and previous not in _FAILING:
            fix = fix_text(cred, status.state, status.reason)  # status.fix is the dashboard's, in English
            if status.state == "invalid":
                lines.append(tr("⚠️ {label}: invalid or expired. {fix}", label=label, fix=fix))
            else:
                lines.append(tr("⚠️ {label}: no access (HTTP {status}). {fix}", label=label,
                                status=status.http_status, fix=fix))
        elif status.state in ("ok", "expiring") and previous in _FAILING:
            lines.append(tr("✅ {label}: working again.", label=label))
        if cred.kind == "claude_login":
            warning = self._expiry_warning(memory, status.expires_at, now)
            if warning:
                lines.append(warning)
        return lines

    def _expiry_warning(
        self, memory: _Memory, expires_at: datetime | None, now: datetime,
    ) -> str | None:
        """The 3-day / 1-day login warning, once each per expiry; none once it has
        expired (that state is `invalid` and has its own line)."""
        if expires_at is None or expires_at <= now:
            return None
        if memory.warned_for != expires_at:  # a re-login starts fresh
            memory.warned_for, memory.warned_days = expires_at, None
        left = expires_at - now
        if left <= LOGIN_LAST_DAY and memory.warned_days != 1:
            memory.warned_days = 1
        elif left <= LOGIN_EXPIRING and memory.warned_days is None:
            memory.warned_days = 3
        else:
            return None
        when = expires_at.astimezone(self._tz).strftime("%Y-%m-%d %H:%M")
        return tr("⏳ The claude login on the host expires {when}. {fix}", when=when, fix=tr(_RELOGIN_FIX))

    async def _send(self, line: str) -> None:
        if self._send_line is None:
            return
        try:
            await self._send_line(line)
        except Exception as e:
            logger.warning("Credential health line not sent: %s", type(e).__name__)
