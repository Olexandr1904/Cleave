"""Tests for main's reviewer wiring: one reviewer context per loaded repo with reviews on, the
reviewer chat, the replies to reviewer messages, and the background tasks (credential health,
reviewer, review jobs, review edits) started and stopped around the daemon."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx
import uvicorn

import dashboard.web
import main
import orchestrator.reviewer.replies as replies_module
import orchestrator.reviewer.runner as runner_module
import orchestrator.reviewer.service as service_module
import orchestrator.reviewer.small_jobs as small_jobs_module
from config.schemas import (
    GitHubConfig,
    GlobalConfig,
    JiraConfig,
    LoadedProject,
    ProjectConfig,
    RepoConfig,
    ReviewerConfig,
    ReviewerGlobalConfig,
    ReviewerJobConfig,
    TelegramConfig,
    TrackerConfig,
    VCSConfig,
)
from health.credentials import CredentialHealth
from integrations.telegram.telegram_adapter import TelegramAdapter
from main import (
    _allowed_chat_ids,
    _allowed_links,
    _reviewed_scopes,
    _reviewer_chat,
    _reviewer_contexts,
    _start_background,
    _start_reviewer,
    _stop_background,
)
from orchestrator.orchestrator import Orchestrator
from orchestrator.reviewer.actions import ReviewerActions
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import ForgeError
from orchestrator.reviewer.models import Post
from orchestrator.reviewer.posting import Posting
from orchestrator.reviewer.reply_llm import ReplyParser
from orchestrator.reviewer.small_jobs import SmallRunnerConfig
from orchestrator.reviewer.store import ReviewerStore
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


def test_reviewed_scopes(tmp_path):
    app, web = _repo("acme-app", reviews=True), _repo("acme-web", reviews=False)
    projects = {"acme": LoadedProject(repos={"app": app, "web": web})}
    on_auth_failure, _ = _reporters()

    contexts = _reviewer_contexts(_global(tmp_path), projects, on_auth_failure)

    assert _reviewed_scopes(contexts) == frozenset({"acme/app"})   # the web repo, reviews off, is left out


def test_reviewed_scopes_empty_in_a_dry_run(tmp_path):
    on = {"acme": LoadedProject(repos={"app": _repo("acme-app", reviews=True)})}
    on_auth_failure, _ = _reporters()

    assert _reviewed_scopes(_reviewer_contexts(_global(tmp_path), on, on_auth_failure, dry_run=True)) == frozenset()


def test_reviewed_scopes_ignore_the_switch(tmp_path):
    # _reviewed_scopes takes contexts only: no Switches argument exists to consult
    import inspect
    assert list(inspect.signature(_reviewed_scopes).parameters) == ["contexts"]


def _jira(url: str) -> ProjectConfig:
    return ProjectConfig(tracker=TrackerConfig(provider="jira", jira=JiraConfig(url=url)))


def test_allowed_links():
    app = _repo("acme-app", reviews=True)
    assert _allowed_links(app, _jira("https://acme.atlassian.net")) == (
        "https://github.com/acme-org/", "https://acme.atlassian.net/")
    assert _allowed_links(app, ProjectConfig(tracker=TrackerConfig(provider="trello"))) == (
        "https://github.com/acme-org/", "https://trello.com/")
    upper = _repo("acme-app", reviews=True, owner="Acme-Org")
    assert _allowed_links(upper, _jira("https://Acme.Atlassian.net")) == (   # lowercased, as body_problems expects
        "https://github.com/acme-org/", "https://acme.atlassian.net/")
    assert _allowed_links(app, _jira("")) == ("https://github.com/acme-org/",)   # a Jira tracker with no URL


def test_contexts_have_poster(tmp_path):
    app = _repo("acme-app", reviews=True)
    projects = {"acme": LoadedProject(config=_jira("https://acme.atlassian.net"), repos={"app": app})}
    on_auth_failure, _ = _reporters()

    [ctx] = _reviewer_contexts(_global(tmp_path), projects, on_auth_failure)

    assert ctx.poster.repo == "acme-org/acme-app"
    assert ctx.poster._cfg is app.reviewer.poster
    assert "tok" not in repr(ctx.poster)
    assert ctx.allowed_links == ("https://github.com/acme-org/", "https://acme.atlassian.net/")
    assert ctx.commit_author == ("Cleave Bot", "cleave@pipeline.local")


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

    async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
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


def test_reviewer_chat_carries_the_translator():
    project = LoadedProject(config=ProjectConfig(telegram=TelegramConfig(default_chat_id="7")))
    translator = object()
    assert _reviewer_chat(_Notifier(), GlobalConfig(), {"acme": project}, translator=translator).translator is translator
    assert _reviewer_chat(_Notifier(), GlobalConfig(), {"acme": project}).translator is None


IGNORED = "  Reviewer: replies are ignored — reviewer.telegram_chat_id is not an allowed chat\n"


def test_reviewer_chat_not_added_to_allowlist(capsys, caplog):
    project = {"acme": LoadedProject(config=ProjectConfig(telegram=TelegramConfig(default_chat_id="7")))}

    def config(default: str, reviewer: str) -> GlobalConfig:
        return GlobalConfig(telegram=TelegramConfig(default_chat_id=default),
                            reviewer=ReviewerGlobalConfig(telegram_chat_id=reviewer))

    with caplog.at_level(logging.WARNING, logger="main"):
        assert _allowed_chat_ids(config("1", "42"), project) == {"1", "7"}   # not widened
    assert capsys.readouterr().out == IGNORED
    assert "reviewer.telegram_chat_id is not an allowed chat" in caplog.text

    caplog.clear()
    assert _allowed_chat_ids(config("42", "42"), project) == {"42", "7"}   # the default chat
    assert _allowed_chat_ids(config("1", "7"), project) == {"1", "7"}   # a project's chat
    assert _allowed_chat_ids(config("1", ""), project) == {"1", "7"}   # no reviewer chat: the operator chat
    assert capsys.readouterr().out == ""
    assert caplog.text == ""


class _Part:
    """The reviewer service, the job runner or the small runner: run(stop) returns once stop is set.
    start() and loop(stop) stand in for a runner's recovery and its loop (run without the recovery);
    `starts` counts the start() calls."""

    def __init__(self):
        self.stops: list[asyncio.Event] = []
        self.starts = 0

    async def run(self, stop):
        self.stops.append(stop)
        await stop.wait()

    async def start(self):
        self.starts += 1

    loop = run


async def test_reviewer_parts_started():
    reviewer, runner, small = _Part(), _Part(), _Part()
    background = _start_reviewer(reviewer, runner, small)   # repos to review: all three are built
    assert list(background) == ["Reviewer", "Review jobs", "Review edits"]
    await asyncio.sleep(0)
    assert reviewer.stops == [background["Reviewer"][1]]
    assert runner.stops == [background["Review jobs"][1]]
    assert small.stops == [background["Review edits"][1]]

    credential_stop = asyncio.Event()
    credential = _start_background("Credential health", credential_stop.wait())
    await _stop_background({"Credential health": (credential, credential_stop), **background}, timeout=1.0)
    for task, _ in background.values():
        assert task.done() and not task.cancelled()   # stopped by its event, not cancelled

    assert _start_reviewer(None, None, None) == {}   # no repo to review: none is built


NAMES = ("Reviewer", "Review jobs", "Review edits")


async def test_reviewer_parts_wait_for_the_first_credential_round():
    ready, parts = asyncio.Event(), (_Part(), _Part(), _Part())
    background = _start_reviewer(*parts, ready=ready, wait=5.0)
    await asyncio.sleep(0.05)
    assert all(p.stops == [] for p in parts)        # no poll, no job before the first round
    ready.set(); await asyncio.sleep(0.01)
    assert [p.stops for p in parts] == [[background[n][1]] for n in NAMES]
    await _stop_background(background, timeout=1.0)


async def test_runners_recover_before_the_first_credential_round():
    ready, parts = asyncio.Event(), (_Part(), _Part(), _Part())
    background = _start_reviewer(*parts, ready=ready, wait=5.0)
    await asyncio.sleep(0.05)
    assert [p.starts for p in parts] == [0, 1, 1]   # the jobs a dead process left running: failed at once
    assert all(p.stops == [] for p in parts)        # the loops wait; the poll has nothing to recover
    ready.set(); await asyncio.sleep(0.01)
    assert [p.stops for p in parts] == [[background[n][1]] for n in NAMES]
    assert [p.starts for p in parts] == [0, 1, 1]   # once
    await _stop_background(background, timeout=1.0)


async def test_reviewer_parts_start_when_the_wait_runs_out():
    parts = (_Part(), _Part(), _Part())
    background = _start_reviewer(*parts, ready=asyncio.Event(), wait=0.05)
    await asyncio.sleep(0.2)
    assert all(len(p.stops) == 1 for p in parts)
    await _stop_background(background, timeout=1.0)


def _wait_ran_out(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "first round not done" in r.getMessage()]


async def test_wait_running_out_is_logged(caplog):
    caplog.set_level(logging.WARNING, logger="main")
    background = _start_reviewer(_Part(), _Part(), _Part(), ready=asyncio.Event(), wait=0.05)
    await asyncio.sleep(0.2)
    assert sorted(_wait_ran_out(caplog)) == sorted(
        f"Credential health: first round not done after 0 s; starting {n} anyway" for n in NAMES)
    await _stop_background(background, timeout=1.0)

    caplog.clear()
    ready = asyncio.Event()
    background = _start_reviewer(_Part(), _Part(), _Part(), ready=ready, wait=0.05)
    ready.set(); await asyncio.sleep(0.2)
    assert _wait_ran_out(caplog) == []                  # the round came first: nothing to say
    await _stop_background(background, timeout=1.0)

    stop = asyncio.Event()
    task = asyncio.create_task(main._when_ready(asyncio.Event(), stop, _Part().run, 0.05))
    await asyncio.sleep(0.2); stop.set(); await task
    assert _wait_ran_out(caplog) == [
        "Credential health: first round not done after 0 s; starting the reviewer anyway"]


async def test_stop_while_waiting_starts_nothing():
    parts = (_Part(), _Part(), _Part())
    background = _start_reviewer(*parts, ready=asyncio.Event(), wait=5.0)
    await _stop_background(background, timeout=1.0)
    assert all(t.done() and not t.cancelled() for t, _ in background.values())
    assert all(p.stops == [] for p in parts)


class _Built(_Part):
    """Stands in for the ReviewerService or the JobRunner main builds, keeping its arguments."""

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.args, self.kwargs = args, kwargs


def _run_main(tmp_path, monkeypatch, contexts, telegram_events=None, *, default_chat="42", reviewer_chat="",
              dry_run=False, with_dashboard=False):
    """main() from start to shutdown, over a config with no project and the dashboard off (on with
    `with_dashboard`: the caller stubs its app and server), with `contexts` as the reviewer's.
    Returns the config directory, what main built as the reviewer service, the job runner and the
    small runner (and the replies, and the command handler as "handler", with Telegram), and the
    names it gave _stop_background.

    With `telegram_events`, Telegram is on (a bot token, `default_chat` as telegram.default_chat_id and
    `reviewer_chat` as reviewer.telegram_chat_id; no request is made): the list gets the replies' start
    and aclose and the polling's start and stop, in order. The replies record the calls the command
    handler makes on them (`calls`), every message an anchor."""
    cfg = tmp_path / "config-live"
    (cfg / "projects").mkdir(parents=True)
    global_yaml = _MINIMAL_GLOBAL_YAML.format(
        ws=tmp_path / "ws", log=tmp_path / "log", db=tmp_path / "events.db", rv=tmp_path / "rv" / "reviewer.db",
    ) + f"dashboard:\n  enabled: {str(with_dashboard).lower()}\n  db_path: {tmp_path / 'events.db'}\n"
    if telegram_events is not None:
        global_yaml = global_yaml.replace("bot_token: ''", "bot_token: '123:not-a-real-bot-token'", 1)
        global_yaml = global_yaml.replace("default_chat_id: ''", f"default_chat_id: '{default_chat}'", 1)
        global_yaml = global_yaml.replace("reviewer:\n", f"reviewer:\n  telegram_chat_id: '{reviewer_chat}'\n", 1)
    (cfg / "global.yaml").write_text(global_yaml, encoding="utf-8")
    built: dict[str, _Built] = {}

    def recorder(name, cls=None):
        def build(*args, **kwargs):
            built[name] = (cls or _Built)(*args, **kwargs)
            return built[name]
        return build

    class Replies(_Built):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls: list[tuple] = []

        async def start(self):
            telegram_events.append("replies.start")

        async def aclose(self):
            telegram_events.append("replies.aclose")

        def is_anchor(self, chat_id, message_id):
            self.calls.append(("is_anchor", chat_id, message_id))
            return True

        async def handle_reply(self, chat_id, message_id, text, sent_at=None):
            self.calls.append(("handle_reply", chat_id, message_id, text))

    def event(name):
        async def record(self):
            telegram_events.append(name)
        return record

    async def no_send(self, chat_id, text, buttons=None, reply_to_message_id=None):
        telegram_events.append(f"send {text}")
        return 1

    def keep_handler(self, handler):
        built["handler"] = handler

    stopped: list[str] = []

    async def stop_background(background, timeout):
        stopped.extend(background)
        await _stop_background(background, timeout)

    async def idle(self, stop):
        self.first_round.set()   # the first round is done at once: the reviewer's parts start
        await stop.wait()

    async def no_tickets(self):
        return None

    monkeypatch.setenv("GRADLE_USER_HOME", str(tmp_path / "gradle"))   # main caps build memory there
    monkeypatch.setattr(main, "_reviewer_contexts", lambda *args, **kwargs: contexts)
    monkeypatch.setattr(main, "_stop_background", stop_background)
    monkeypatch.setattr(service_module, "ReviewerService", recorder("service"))
    monkeypatch.setattr(runner_module, "JobRunner", recorder("runner"))
    monkeypatch.setattr(small_jobs_module, "SmallJobRunner", recorder("small"))
    monkeypatch.setattr(replies_module, "ReviewerReplies", recorder("replies", Replies))
    monkeypatch.setattr(TelegramAdapter, "start_polling", event("start_polling"))
    monkeypatch.setattr(TelegramAdapter, "stop_polling", event("stop_polling"))
    monkeypatch.setattr(TelegramAdapter, "send_message", no_send)
    monkeypatch.setattr(TelegramAdapter, "set_command_handler", keep_handler)
    monkeypatch.setattr(CredentialHealth, "run", idle)
    monkeypatch.setattr(Orchestrator, "run", no_tickets)   # the daemon shuts down at once
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(root, "handlers", list(before))   # main adds its log file's handler
    try:
        assert main.main(["--config", str(cfg), *(["--dry-run"] if dry_run else [])]) == 0
    finally:
        for handler in root.handlers:
            if handler not in before:
                handler.close()
    return cfg, built, stopped


class _NoServer:
    """Stands in for uvicorn.Server: serves nothing."""

    def __init__(self, config):
        self.should_exit = False

    async def serve(self):
        return None


def test_main_gives_the_dashboard_the_replies_action_layer(tmp_path, monkeypatch):
    """A command from the dashboard runs through the layer the replies run theirs through: one Posting for both."""
    apps: list[dict] = []
    monkeypatch.setattr(dashboard.web, "create_app", lambda *args, **kwargs: apps.append(kwargs) or "app")
    monkeypatch.setattr(uvicorn, "Config", lambda app, **kwargs: app)
    monkeypatch.setattr(uvicorn, "Server", _NoServer)
    _, built, _ = _run_main(tmp_path, monkeypatch, [SimpleNamespace(scope="acme/app")], telegram_events=[],
                            with_dashboard=True)
    [kwargs] = apps
    assert kwargs["reviewer"] is built["service"]
    assert isinstance(kwargs["reviewer_actions"], ReviewerActions)
    assert kwargs["reviewer_actions"] is built["replies"].kwargs["actions"]
    assert kwargs["reviewer_replies"] is built["replies"]


def test_main_builds_the_runner_with_reviewer_contexts(tmp_path, monkeypatch, capsys):
    ctx = SimpleNamespace(scope="acme/app")
    cfg, built, stopped = _run_main(tmp_path, monkeypatch, [ctx])

    assert "  Reviewer: 1 repo(s), jobs on\n" in capsys.readouterr().out   # no Telegram: no posting from it
    service, runner, small = built["service"], built["runner"], built["small"]
    contexts, store, switches, failing, report_auth_failure, chat, llm, trackers, config = runner.args
    assert contexts == [ctx]
    assert store is service.args[1] and switches is service.args[2]   # the service's own
    compared = service.kwargs["compared_tickets"]   # the orchestrator's, read on each poll
    assert isinstance(compared.__self__, Orchestrator) and compared.__name__ == "compared_tickets"
    assert isinstance(failing.__self__, CredentialHealth) and failing.__name__ == "failing"
    assert report_auth_failure.__self__ is failing.__self__
    assert report_auth_failure.__name__ == "report_auth_failure"
    assert chat is None and service.args[4] is None   # no Telegram configured
    assert type(llm).__name__ == "ClaudeCodeAdapter" and trackers == {}
    assert config.jobs_dir == tmp_path / "rv" / "jobs"
    assert config.secret_paths == [Path(main.__file__).parent / ".env", cfg.absolute()]
    assert config.ledgers_dir == tmp_path / "rv" / "ledgers"   # the retention sweep runs
    assert runner.kwargs["event_bus"] is service.kwargs["event_bus"] is not None
    # The edit and draft jobs: the same store and chat, the review jobs' directory and secret paths.
    small_store, small_chat, small_llm, small_config = small.args
    assert small_store is store and small_chat is chat
    assert type(small_llm).__name__ == "ClaudeCodeAdapter"
    assert small_config == SmallRunnerConfig(config.jobs_dir, config.secret_paths, config.jobs)
    assert small.kwargs["event_bus"] is runner.kwargs["event_bus"]
    assert "replies" not in built   # no Telegram: nothing to reply to
    # The "Review jobs" and "Review edits" tasks ran next to the reviewer's, stopped with it by their events.
    assert stopped == ["Credential health", "Reviewer", "Review jobs", "Review edits"]
    assert [s.is_set() for s in (*service.stops, *runner.stops, *small.stops)] == [True, True, True]


def test_main_wires_replies_with_telegram(tmp_path, monkeypatch, capsys):
    ctx = SimpleNamespace(scope="acme/app")
    events: list[str] = []
    _, built, stopped = _run_main(tmp_path, monkeypatch, [ctx], telegram_events=events)

    out = capsys.readouterr().out
    assert "  Reviewer: 1 repo(s), jobs on, posting from Telegram\n" in out
    assert "replies are ignored" not in out   # no reviewer chat: the operator chat, which is allowed
    service, runner, small, replies = built["service"], built["runner"], built["small"], built["replies"]
    contexts, store, reviewer, chat, posting, small_runner, parser = replies.args
    assert contexts == [ctx] and reviewer is service and small_runner is small
    assert store is service.args[1] is runner.args[1] is small.args[0]
    # One reviewer chat, in the operator chat, anchoring every message in the store.
    assert chat is runner.args[5] is small.args[1] and service.args[4] == chat.send
    assert (chat.chat_id, chat.store) == ("42", store)
    assert isinstance(posting, Posting) and posting._store is store
    assert posting._ledgers_dir == runner.args[8].ledgers_dir == tmp_path / "rv" / "ledgers"
    assert posting._work_dir == runner.args[8].work_root == tmp_path / "rv" / "work"   # a push's worktree
    assert isinstance(parser, ReplyParser) and type(parser._llm).__name__ == "ClaudeCodeAdapter"
    assert parser._job_cfg == ReviewerJobConfig("sonnet", "", 60)   # reviewer.jobs.command
    assert parser._work_dir == tmp_path / "rv" / "command"
    language_of = runner.kwargs["language_of"]
    actions = replies.kwargs.pop("actions")
    assert replies.kwargs == {"credential_failing": runner.args[3], "language_of": language_of}
    # The replies run their commands through the layer the dashboard gets too: one Posting for both channels.
    assert isinstance(actions, ReviewerActions) and actions.posting is posting
    # Every part of the reviewer writes in the project's Telegram language (English by default), and the chat
    # translates the model-written text of its messages.
    assert small.kwargs["language_of"] is service.kwargs["language_of"] is language_of
    assert language_of("acme") == language_of("") == "en"
    assert chat.translator is not None and chat.translator._model == "haiku"
    # A reply to a reviewer message goes to the replies, never to the ticket pipeline's intent parser; the
    # reviewer reads the allowlist's chats only.
    assert built["handler"]._reviewer is replies
    assert built["handler"]._allowed_chat_ids == built["handler"]._reviewer_chat_ids == {"42"}
    # Interrupted posts are announced before any reply arrives; the tasks are cancelled while Telegram runs.
    assert events == ["replies.start", "start_polling", "replies.aclose", "stop_polling"]
    assert stopped == ["Credential health", "Reviewer", "Review jobs", "Review edits"]


def test_main_replies_off_without_reviewer_contexts(tmp_path, monkeypatch):
    events: list[str] = []
    _, built, stopped = _run_main(tmp_path, monkeypatch, [], telegram_events=events)

    replies = built["replies"]
    contexts, store, reviewer, chat, posting, small, parser = replies.args
    assert contexts == [] and (reviewer, small, parser) == (None, None, None)   # it answers "off"
    assert chat.store is store and isinstance(posting, Posting)
    assert built["handler"]._reviewer is replies
    assert events == ["replies.start", "start_polling", "replies.aclose", "stop_polling"]
    assert stopped == ["Credential health"]


REPLIES_RUN = [("is_anchor", "42", 100), ("handle_reply", "42", 100, "post 1")]


@pytest.mark.parametrize(("default_chat", "posting", "handled", "calls"), [
    # No default or project chat: the allowlist is empty and the bot is locked down, so the reply is turned away
    # before anything reads it.
    ("", False, False, []),
    ("1", False, False, []),   # another chat is allowed, not this one: the allowlist turns the reply away
    ("42", True, True, REPLIES_RUN),   # the reviewer chat is the default chat: the reply runs
])
def test_reviewer_path_needs_an_allowed_chat(tmp_path, monkeypatch, capsys, default_chat, posting, handled, calls):
    events: list[str] = []
    _, built, _ = _run_main(tmp_path, monkeypatch, [SimpleNamespace(scope="acme/app")], telegram_events=events, default_chat=default_chat,
                            reviewer_chat="42")

    out = capsys.readouterr().out
    assert (IGNORED in out) is not posting
    # ", posting from Telegram" only when the reviewer reads replies in its own chat
    line = "  Reviewer: 1 repo(s), jobs on, posting from Telegram\n" if posting else "  Reviewer: 1 repo(s), jobs on\n"
    assert line in out
    replies, handler = built["replies"], built["handler"]
    assert asyncio.run(handler.handle_reply(100, "post 1", "42")) is handled
    assert replies.calls == calls


def test_empty_allowlist_locks_the_bot(tmp_path, monkeypatch):
    """No default or project chat: the bot accepts no message from any chat (docs/telegram.md, Allowlist)."""
    events: list[str] = []
    _, built, _ = _run_main(tmp_path, monkeypatch, [SimpleNamespace(scope="acme/app")], telegram_events=events, default_chat="",
                            reviewer_chat="42")
    handler = built["handler"]
    assert handler._allowed_chat_ids == set()

    parsed: list[str] = []

    async def parse(text, *args, **kwargs):
        parsed.append(text)

    handler._intent_parser.parse = parse
    asyncio.run(handler.handle_message("approve", "999"))
    assert parsed == []


def test_dry_run_starts_no_replies(tmp_path, monkeypatch):
    """A dry run writes nothing and sends no line at start: no post is recovered or announced."""
    events: list[str] = []
    _, built, _ = _run_main(tmp_path, monkeypatch, [], telegram_events=events, dry_run=True)

    assert built["handler"]._reviewer is built["replies"]   # a reply is still kept from the intent parser
    assert events == ["start_polling", "replies.aclose", "stop_polling"]


def _cut_short_push(tmp_path) -> ReviewerStore:
    """The reviewer store main opens, holding a push the last daemon left `posting`."""
    store = ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))
    store.save_post(Post("p-cut", "j1", "acme/app", 1712, "a" * 40, "push", "posting", "2026-11-23T10:00:00+00:00"))
    return store


@pytest.mark.parametrize("dry_run", [False, True])
def test_without_telegram_the_posts_a_restart_cut_short_are_recovered_before_the_dashboard_serves(
        tmp_path, monkeypatch, caplog, dry_run):
    """The dashboard posts on a daemon with no Telegram too: a post the last one left `posting` is marked
    interrupted (and logged) before the dashboard serves, else its PR's fixes are refused and the board shows
    "Posting" for good. A dry run writes nothing."""
    store = _cut_short_push(tmp_path)
    served: list[str] = []

    class Server(_NoServer):
        async def serve(self):
            served.append(store.post("p-cut").state)

    monkeypatch.setattr(dashboard.web, "create_app", lambda *args, **kwargs: "app")
    monkeypatch.setattr(uvicorn, "Config", lambda app, **kwargs: app)
    monkeypatch.setattr(uvicorn, "Server", Server)
    with caplog.at_level(logging.WARNING, logger="main"):
        _run_main(tmp_path, monkeypatch, [SimpleNamespace(scope="acme/app")], with_dashboard=True, dry_run=dry_run)

    expected = "posting" if dry_run else "interrupted"
    assert served == [expected] and store.post("p-cut").state == expected
    recovered = [r.getMessage() for r in caplog.records if "interrupted by a restart" in r.getMessage()]
    assert recovered == ([] if dry_run else ["Reviewer push on acme/app#1712 interrupted by a restart: "
                                             "marked interrupted"])


def test_main_gives_the_reviewer_chat_the_store(tmp_path, monkeypatch):
    """Every message the reviewer sends is a reply anchor: the daemon's chat writes it to the reviewer store."""
    real = main._reviewer_chat
    with_telegram = GlobalConfig(reviewer=ReviewerGlobalConfig(telegram_chat_id="42"))
    monkeypatch.setattr(main, "_reviewer_chat", lambda notifier, global_config, projects, **kwargs:
                        real(_Notifier(), with_telegram, projects, **kwargs))   # no Telegram configured
    _, built, _ = _run_main(tmp_path, monkeypatch, [SimpleNamespace(scope="acme/app")])
    service, runner = built["service"], built["runner"]
    chat = runner.args[5]
    assert chat.store is service.args[1] is runner.args[1]
    assert service.args[4] == chat.send


def test_main_builds_no_runner_without_reviewer_contexts(tmp_path, monkeypatch, capsys):
    _, built, stopped = _run_main(tmp_path, monkeypatch, [])   # no Telegram either: no replies

    assert built == {}
    assert stopped == ["Credential health"]
    assert "Reviewer:" not in capsys.readouterr().out


def test_main_hands_the_orchestrator_the_reviewed_scopes(tmp_path, monkeypatch):
    handed = []
    monkeypatch.setattr(Orchestrator, "set_reviewed_repos", lambda self, scopes: handed.append(scopes))
    _run_main(tmp_path, monkeypatch, [SimpleNamespace(scope="acme/app")])

    assert handed == [frozenset({"acme/app"})]


def test_main_hands_the_orchestrator_no_scopes_without_reviewer_contexts(tmp_path, monkeypatch):
    handed = []
    monkeypatch.setattr(Orchestrator, "set_reviewed_repos", lambda self, scopes: handed.append(scopes))
    _run_main(tmp_path, monkeypatch, [])

    assert handed == [frozenset()]


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
