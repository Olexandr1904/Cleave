"""Tests for main's reviewer wiring: one reviewer context per loaded repo with reviews on, the
reviewer chat, and the background tasks (credential health, reviewer, review jobs) started and
stopped around the daemon."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest
import respx

import main
import orchestrator.reviewer.runner as runner_module
import orchestrator.reviewer.service as service_module
from config.schemas import (
    GitHubConfig,
    GlobalConfig,
    LoadedProject,
    ProjectConfig,
    RepoConfig,
    ReviewerConfig,
    ReviewerGlobalConfig,
    TelegramConfig,
    VCSConfig,
)
from health.credentials import CredentialHealth
from main import (
    _review_anchor,
    _reviewer_chat,
    _reviewer_contexts,
    _start_background,
    _start_reviewer,
    _stop_background,
)
from orchestrator.orchestrator import Orchestrator
from orchestrator.reviewer.forge_github import ForgeError
from tests.unit.test_main_startup import _MINIMAL_GLOBAL_YAML


def _repo(name: str, *, reviews: bool, owner: str = "acme-org", token: str = "tok") -> RepoConfig:
    cfg = RepoConfig()
    github = GitHubConfig(token=token, owner=owner, repo=name)
    cfg.vcs = VCSConfig(provider="github", github=github)
    cfg.reviewer = ReviewerConfig(enabled=reviews, base_branch="develop")
    return cfg


def _global(tmp_path) -> GlobalConfig:
    return GlobalConfig(reviewer=ReviewerGlobalConfig(db_path=str(tmp_path / "rv" / "reviewer.db")))


def _reporters():
    """An on_auth_failure factory like main's, recording (key, status) for each report."""
    reported: list[tuple[str, int]] = []
    return (lambda key: lambda status: reported.append((key, status))), reported


def test_reviewer_contexts(tmp_path):
    global_config = _global(tmp_path)
    app, web = _repo("acme-app", reviews=True), _repo("acme-web", reviews=False)
    projects = {"acme": LoadedProject(repos={"app": app, "web": web})}
    on_auth_failure, _ = _reporters()

    contexts = _reviewer_contexts(global_config, projects, on_auth_failure)

    assert len(contexts) == 1
    ctx = contexts[0]
    assert ctx.scope == "acme/app"
    assert ctx.clone.path == tmp_path / "rv" / "repos" / "acme" / "app"
    assert ctx.clone.url == "https://github.com/acme-org/acme-app.git"
    assert ctx.readiness.repo == "acme-org/acme-app"
    assert ctx.forge._adapter._repo_path == "/repos/acme-org/acme-app"
    assert "tok" not in repr(ctx)


def test_reviewer_contexts_skip_a_repo_without_a_forge_token(tmp_path, caplog):
    projects = {"acme": LoadedProject(repos={"app": _repo("acme-app", reviews=True, token="")})}
    on_auth_failure, _ = _reporters()
    with caplog.at_level(logging.WARNING):
        assert _reviewer_contexts(_global(tmp_path), projects, on_auth_failure) == []
    assert "acme/app" in caplog.text and "no forge token" in caplog.text


@respx.mock
async def test_reviewer_contexts_same_repo_id_in_two_projects(tmp_path):
    projects = {
        "acme": LoadedProject(repos={"app": _repo("acme-app", reviews=True)}),
        "beta": LoadedProject(repos={"app": _repo("beta-app", reviews=True, owner="beta-org")}),
    }
    on_auth_failure, reported = _reporters()

    acme, beta = _reviewer_contexts(_global(tmp_path), projects, on_auth_failure)

    assert (acme.scope, beta.scope) == ("acme/app", "beta/app")
    assert acme.clone.path == tmp_path / "rv" / "repos" / "acme" / "app"
    assert beta.clone.path == tmp_path / "rv" / "repos" / "beta" / "app"
    assert beta.clone.url == "https://github.com/beta-org/beta-app.git"
    # Each forge reads its own owner/repo and reports a 401 on its own credential key.
    respx.get("https://api.github.com/repos/acme-org/acme-app/pulls").respond(401)
    respx.get("https://api.github.com/repos/beta-org/beta-app/pulls").respond(401)
    for ctx in (acme, beta):
        with pytest.raises(ForgeError, match="^GitHub 401$"):
            await ctx.forge.open_prs()
    assert reported == [("forge:acme/app", 401), ("forge:beta/app", 401)]


def test_dry_run_builds_no_reviewer(tmp_path, capsys):
    built = []
    def on_auth_failure(key):
        built.append(key)
        return lambda status: None
    on = {"acme": LoadedProject(repos={"app": _repo("acme-app", reviews=True)})}
    off = {"acme": LoadedProject(repos={"app": _repo("acme-app", reviews=False)})}

    assert _reviewer_contexts(_global(tmp_path), on, on_auth_failure, dry_run=True) == []
    assert capsys.readouterr().out == "  Reviewer: off (dry-run)\n"
    assert _reviewer_contexts(_global(tmp_path), off, on_auth_failure, dry_run=True) == []
    assert capsys.readouterr().out == ""
    assert built == []   # no adapter, so no forge read either


class _Notifier:
    def __init__(self, fail: Exception | None = None):
        self.sent: list[tuple[str, str]] = []
        self._fail = fail

    async def send_message(self, chat_id, text):
        if self._fail:
            raise self._fail
        self.sent.append((chat_id, text))
        return 500 + len(self.sent)  # the Telegram message id


async def test_reviewer_chat_target():
    reviewer_chat = GlobalConfig(reviewer=ReviewerGlobalConfig(telegram_chat_id="42"))
    project = LoadedProject(config=ProjectConfig(telegram=TelegramConfig(default_chat_id="7")))
    assert _reviewer_chat(None, reviewer_chat, {"acme": project}) is None   # no Telegram
    assert _reviewer_chat(_Notifier(), GlobalConfig(), {}) is None   # no chat id

    notifier = _Notifier()
    chat = _reviewer_chat(notifier, reviewer_chat, {"acme": project})
    assert chat.chat_id == "42"   # the reviewer chat first
    assert await chat.send("line") == 501
    assert notifier.sent == [("42", "line")]

    fallback = _reviewer_chat(_Notifier(), GlobalConfig(), {"acme": project})
    assert fallback.chat_id == "7"   # else the operator chat
    assert await fallback.send("line") == 501
    broken = _reviewer_chat(_Notifier(fail=RuntimeError("down")), GlobalConfig(), {"acme": project})
    with pytest.raises(RuntimeError):
        await broken.send("line")


def test_review_anchor_reads_the_reviewer_store(tmp_path):
    from orchestrator.reviewer.db import connect
    from orchestrator.reviewer.store import ReviewerStore
    store = ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))
    store.add_anchor("-1001", 100, "job1", "2026-11-20T10:00:00+00:00")
    anchor = _review_anchor(store)
    assert anchor("-1001", 100) is True
    assert anchor("-1001", 101) is False and anchor("-1002", 100) is False
    assert _review_anchor(None) is None   # no reviewer: replies go where they always went


class _Part:
    """The reviewer service or the job runner: run(stop) returns once stop is set."""

    def __init__(self):
        self.stops: list[asyncio.Event] = []

    async def run(self, stop):
        self.stops.append(stop)
        await stop.wait()


async def test_runner_wired_next_to_reviewer():
    reviewer, runner = _Part(), _Part()
    background = _start_reviewer(reviewer, runner)   # repos to review: both are built
    assert list(background) == ["Reviewer", "Review jobs"]
    await asyncio.sleep(0)
    assert reviewer.stops == [background["Reviewer"][1]]
    assert runner.stops == [background["Review jobs"][1]]

    credential_stop = asyncio.Event()
    credential = _start_background("Credential health", credential_stop.wait())
    await _stop_background({"Credential health": (credential, credential_stop), **background}, timeout=1.0)
    for task, _ in background.values():
        assert task.done() and not task.cancelled()   # stopped by its event, not cancelled

    assert _start_reviewer(None, None) == {}   # no repo to review: neither is built


class _Built(_Part):
    """Stands in for the ReviewerService or the JobRunner main builds, keeping its arguments."""

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.args, self.kwargs = args, kwargs


def _run_main(tmp_path, monkeypatch, contexts):
    """main() from start to shutdown, over a config with no project and the dashboard off, with
    `contexts` as the reviewer's. Returns the config directory, what main built as the reviewer
    service and the job runner, and the names it gave _stop_background."""
    cfg = tmp_path / "config-live"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "global.yaml").write_text(_MINIMAL_GLOBAL_YAML.format(
        ws=tmp_path / "ws", log=tmp_path / "log", db=tmp_path / "events.db", rv=tmp_path / "rv" / "reviewer.db",
    ) + f"dashboard:\n  enabled: false\n  db_path: {tmp_path / 'events.db'}\n", encoding="utf-8")
    built: dict[str, _Built] = {}

    def recorder(name):
        def build(*args, **kwargs):
            built[name] = _Built(*args, **kwargs)
            return built[name]
        return build

    stopped: list[str] = []

    async def stop_background(background, timeout):
        stopped.extend(background)
        await _stop_background(background, timeout)

    async def idle(self, stop):
        await stop.wait()

    async def no_tickets(self):
        return None

    monkeypatch.setenv("GRADLE_USER_HOME", str(tmp_path / "gradle"))   # main caps build memory there
    monkeypatch.setattr(main, "_reviewer_contexts", lambda *args, **kwargs: contexts)
    monkeypatch.setattr(main, "_stop_background", stop_background)
    monkeypatch.setattr(service_module, "ReviewerService", recorder("service"))
    monkeypatch.setattr(runner_module, "JobRunner", recorder("runner"))
    monkeypatch.setattr(CredentialHealth, "run", idle)
    monkeypatch.setattr(Orchestrator, "run", no_tickets)   # the daemon shuts down at once
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(root, "handlers", list(before))   # main adds its log file's handler
    try:
        assert main.main(["--config", str(cfg)]) == 0
    finally:
        for handler in root.handlers:
            if handler not in before:
                handler.close()
    return cfg, built, stopped


def test_main_builds_the_runner_with_reviewer_contexts(tmp_path, monkeypatch, capsys):
    ctx = object()
    cfg, built, stopped = _run_main(tmp_path, monkeypatch, [ctx])

    assert "  Reviewer: 1 repo(s), jobs on\n" in capsys.readouterr().out
    service, runner = built["service"], built["runner"]
    contexts, store, switches, failing, report_auth_failure, chat, llm, trackers, config = runner.args
    assert contexts == [ctx]
    assert store is service.args[1] and switches is service.args[2]   # the service's own
    assert isinstance(failing.__self__, CredentialHealth) and failing.__name__ == "failing"
    assert report_auth_failure.__self__ is failing.__self__
    assert report_auth_failure.__name__ == "report_auth_failure"
    assert chat is None and service.args[4] is None   # no Telegram configured
    assert type(llm).__name__ == "ClaudeCodeAdapter" and trackers == {}
    assert config.jobs_dir == tmp_path / "rv" / "jobs"
    assert config.secret_paths == [Path(main.__file__).parent / ".env", cfg.absolute()]
    assert runner.kwargs["event_bus"] is service.kwargs["event_bus"] is not None
    # The "Review jobs" task ran next to the reviewer's and was stopped with it, by its event.
    assert stopped == ["Credential health", "Reviewer", "Review jobs"]
    assert [s.is_set() for s in (*service.stops, *runner.stops)] == [True, True]


def test_main_builds_no_runner_without_reviewer_contexts(tmp_path, monkeypatch, capsys):
    _, built, stopped = _run_main(tmp_path, monkeypatch, [])

    assert built == {}
    assert stopped == ["Credential health"]
    assert "Reviewer:" not in capsys.readouterr().out


async def _forever():
    await asyncio.Event().wait()


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_background_task_that_raises_is_logged_once_by_class(caplog):
    async def fails():
        raise RuntimeError("text-that-is-not-ours")

    task = _start_background("Reviewer", fails())
    await asyncio.wait({task})
    await asyncio.sleep(0)
    [record] = _errors(caplog)
    assert "Reviewer stopped unexpectedly" in record.getMessage()
    assert "RuntimeError at " in record.getMessage()
    assert "text-that-is-not-ours" not in caplog.text


async def test_background_task_cancelled_or_returned_is_not_logged(caplog):
    async def returns():
        return None

    cancelled = _start_background("Reviewer", _forever())
    returned = _start_background("Credential health", returns())
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.wait({cancelled, returned})
    await asyncio.sleep(0)
    assert cancelled.cancelled() and returned.result() is None
    assert _errors(caplog) == []


async def test_stop_background_continues_past_a_task_that_raised():
    async def raises_on_stop(stop):
        await stop.wait()
        raise RuntimeError("check failed")

    async def stops(stop):
        await stop.wait()
        return "stopped"

    first_stop, second_stop = asyncio.Event(), asyncio.Event()
    first = _start_background("Credential health", raises_on_stop(first_stop))
    second = _start_background("Reviewer", stops(second_stop))
    await _stop_background(
        {"Credential health": (first, first_stop), "Reviewer": (second, second_stop)},
        timeout=1.0,
    )
    assert isinstance(first.exception(), RuntimeError)
    assert second.result() == "stopped"


async def test_stop_background_one_budget_for_all(caplog):
    caplog.set_level(logging.WARNING)
    first = _start_background("Credential health", _forever())
    second = _start_background("Reviewer", _forever())
    started = time.monotonic()
    await _stop_background(
        {"Credential health": (first, asyncio.Event()), "Reviewer": (second, asyncio.Event())},
        timeout=0.5,
    )
    assert time.monotonic() - started < 0.9  # one budget for both, not one each
    assert first.cancelled() and second.cancelled()
    assert "Credential health did not stop within" in caplog.text
    assert "Reviewer did not stop within" in caplog.text


async def test_stop_background_without_a_reviewer():
    stop = asyncio.Event()
    task = _start_background("Credential health", stop.wait())
    await _stop_background(
        {"Credential health": (task, stop), "Reviewer": (None, asyncio.Event())}, timeout=1.0,
    )
    assert task.done() and not task.cancelled()
