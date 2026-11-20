"""Tests for main's skill-flow wiring: one skill repo per loaded repo with ticket_flow both, the objects main builds
around them (one store and one chat for all), the intake on the orchestrator, the replies on the command handler, the
comparison keeper and board, and the runner's and the keeper's background tasks started and stopped around the daemon."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import main
import orchestrator.reviewer.db as db_module
import orchestrator.skillflow.board as board_module
import orchestrator.skillflow.intake as intake_module
import orchestrator.skillflow.keeper as keeper_module
import orchestrator.skillflow.replies as replies_module
import orchestrator.skillflow.runner as runner_module
from config.schemas import GitHubConfig, GlobalConfig, LoadedProject, RepoConfig, TelegramConfig, VCSConfig
from health.credentials import CredentialHealth
from integrations.base.tracker import TicketData
from integrations.telegram.telegram_adapter import TelegramAdapter
from main import _build_vcs_adapter, _skill_repos, _start_skill, _stop_background
from orchestrator.orchestrator import Orchestrator
from orchestrator.reviewer.db import connect
from orchestrator.skillflow.chat import SkillChat
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.skillflow.intake import SkillIntake
from orchestrator.skillflow.keeper import ComparisonKeeper
from orchestrator.skillflow.replies import SkillOff, SkillReplies
from orchestrator.skillflow.sides import Workspaces
from orchestrator.skillflow.store import SkillStore
from orchestrator.switches import Switches
from tests.unit.test_main_startup import _MINIMAL_GLOBAL_YAML
from workspace.workspace import Stage

NOW = "2026-11-17T10:00:00+00:00"


def _initialised() -> Orchestrator:
    return Orchestrator(
        global_config=GlobalConfig(), projects={}, registry=MagicMock(), workflow=MagicMock(),
        workspace_manager=MagicMock(discover_workspaces=lambda: []), agent_runtime=MagicMock(),
        default_model_provider=lambda: "sonnet",
    )


def _without_init() -> Orchestrator:
    """As the tests that skip __init__ build it."""
    return Orchestrator.__new__(Orchestrator)


@pytest.fixture(params=[_initialised, _without_init], ids=["init", "no-init"])
def orch(request):
    return request.param()


def test_build_slot_is_the_runtimes_semaphore(orch):
    assert orch.build_slot is orch._runtime._build_semaphore


def _repo(name: str, flow: str, *, owner: str = "acme-org", token: str | None = None, chat: str = "7") -> RepoConfig:
    """A GitHub repo whose project's Telegram chat is `chat` (the loader copies the project's chat into each repo)."""
    cfg = RepoConfig(ticket_flow=flow, telegram=TelegramConfig(default_chat_id=chat))
    github = GitHubConfig(token=f"tok-{name}" if token is None else token, owner=owner, repo=f"acme-{name}")
    cfg.vcs = VCSConfig(provider="github", default_branch="develop", github=github)
    return cfg


def projects_with(both=(), staged=(), project: str = "acme") -> dict[str, LoadedProject]:
    repos = {name: _repo(name, "both") for name in both} | {name: _repo(name, "staged") for name in staged}
    return {project: LoadedProject(repos=repos)}


def adapters_for(projects: dict[str, LoadedProject]) -> dict:
    """As main builds them: keyed by repo id, each with its repo config; none for a repo without a token."""
    return {repo_id: (adapter, cfg) for project in projects.values() for repo_id, cfg in project.repos.items()
            if (adapter := _build_vcs_adapter(cfg)) is not None}


def test_skill_repos_only_for_both(tmp_path):
    projects = projects_with(both=["app"], staged=["web"])
    repos = _skill_repos(projects, adapters_for(projects), tmp_path, dry_run=False, telegram=True)
    assert [r.scope for r in repos] == ["acme/app"]
    assert repos[0].checkout.path == tmp_path / "repos" / "acme" / "app"
    assert repos[0].checkout.base_branch == "develop"


@pytest.mark.parametrize("kwargs, line", [({"dry_run": True, "telegram": True}, "Skill flow: off (dry-run)"),
                                          ({"dry_run": False, "telegram": False}, "Skill flow: off (no Telegram)")])
def test_skill_flow_off(tmp_path, capsys, kwargs, line):
    projects = projects_with(both=["app"])
    assert _skill_repos(projects, adapters_for(projects), tmp_path, **kwargs) == []
    assert line in capsys.readouterr().out


def test_nothing_is_printed_without_a_both_repo(tmp_path, capsys):
    projects = projects_with(staged=["web"])
    assert _skill_repos(projects, adapters_for(projects), tmp_path, dry_run=True, telegram=False) == []
    assert "Skill flow" not in capsys.readouterr().out


def test_a_repo_without_a_forge_adapter_is_skipped(tmp_path, caplog):
    projects = projects_with(both=["app"])
    assert _skill_repos(projects, {}, tmp_path, dry_run=False, telegram=True) == []
    assert "no forge token" in caplog.text


def test_a_repo_without_a_telegram_chat_is_skipped(tmp_path, capsys, caplog):
    """A run nobody can be told about is never made: no project chat and no global chat, no skill repo."""
    projects = {"acme": LoadedProject(repos={"app": _repo("app", "both", chat=""), "web": _repo("web", "both")})}
    with caplog.at_level(logging.WARNING, logger="main"):
        repos = _skill_repos(projects, adapters_for(projects), tmp_path, dry_run=False, telegram=True)
    assert [r.scope for r in repos] == ["acme/web"]
    assert "  Skill flow: off for acme/app (no Telegram chat)\n" in capsys.readouterr().out
    assert "acme/app" in caplog.text and "Telegram chat" in caplog.text

    # The global chat is a chat too.
    repos = _skill_repos(projects, adapters_for(projects), tmp_path, dry_run=False, telegram=True,
                         global_chat_id="42")
    assert [r.scope for r in repos] == ["acme/app", "acme/web"]
    assert "Skill flow: off" not in capsys.readouterr().out


def test_the_checkout_and_the_forge_adapter_hold_one_token(tmp_path):
    """The checkout's redact masks only its own token, and the runner cleans the forge's error texts with it."""
    projects = projects_with(both=["app"])
    adapters = adapters_for(projects)
    [repo] = _skill_repos(projects, adapters, tmp_path, dry_run=False, telegram=True)
    assert repo.vcs is adapters["app"][0]
    assert repo.checkout._token == repo.vcs._token == "tok-app"
    assert "tok-app" not in repo.checkout.redact("GitHub said: tok-app")
    assert repo.checkout._url == "https://github.com/acme-org/acme-app.git"


def test_a_repo_id_another_project_uses_too_is_skipped(tmp_path, caplog):
    """main keys the forge adapters by repo id alone: the same id in a second project keeps only that project's
    adapter, whose token and repo are not this repo's."""
    beta = LoadedProject(repos={"app": _repo("app", "both", owner="beta-org", token="tok-beta")})
    projects = {**projects_with(both=["app"]), "beta": beta}
    adapters = adapters_for(projects)   # "app" is beta's
    with caplog.at_level(logging.WARNING, logger="main"):
        repos = _skill_repos(projects, adapters, tmp_path, dry_run=False, telegram=True)
    assert [r.scope for r in repos] == ["beta/app"]
    assert repos[0].vcs is adapters["app"][0] and repos[0].checkout._token == "tok-beta"
    assert "acme/app" in caplog.text and "no forge token" not in caplog.text


class FakeRunner:
    """Stands in for the SkillRunner, keeping the arguments it was built with: start() (the recovery) and
    loop(stop) count their calls, and the loop returns once stop is set."""

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.started = self.looped = 0
        self.stops: list[asyncio.Event] = []

    def wake(self) -> None:
        pass

    async def start(self):
        self.started += 1

    async def loop(self, stop):
        self.looped += 1
        self.stops.append(stop)
        await stop.wait()


async def test_start_skill_recovers_at_once_and_loops_when_ready():
    runner, ready = FakeRunner(), asyncio.Event()
    tasks = _start_skill(runner, ready, wait=60)
    await asyncio.sleep(0)
    assert runner.started == 1 and runner.looped == 0
    ready.set(); await asyncio.sleep(0.01)
    assert runner.looped == 1
    await _stop_background(tasks, timeout=1.0)


async def test_start_skill_stops_by_its_event():
    runner, ready = FakeRunner(), asyncio.Event()
    tasks = _start_skill(runner, ready, wait=60)
    ready.set(); await asyncio.sleep(0.01)
    assert list(tasks) == ["Skill runs"]
    [(task, stop)] = tasks.values()
    assert runner.stops == [stop]
    await _stop_background(tasks, timeout=1.0)
    assert task.done() and not task.cancelled()   # stopped by its event, not cancelled


def test_start_skill_without_a_runner_starts_nothing():
    assert _start_skill(None, asyncio.Event()) == {}


class _Built:
    """Stands in for the SkillIntake or the SkillReplies main builds, keeping its arguments."""

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


_SKILL_FLOW = {"bug_skill": "fix-this", "task_skill": "implement-this", "notes_dir": "ai_instructions/{key}",
               "worktree_dir": ".claude/worktrees/{key}"}


def _repo_yaml(name: str, flow: str, token: str) -> str:
    return yaml.safe_dump({
        "repo": {"id": name, "name": f"acme-{name}"},
        "vcs": {"provider": "github", "default_branch": "develop",
                "github": {"token": token, "owner": "acme-org", "repo": f"acme-{name}"}},
        "ticket_flow": flow, "skill_flow": _SKILL_FLOW,
    })


def _run_main(tmp_path, monkeypatch, *, flow: str = "both", telegram: bool = True, dry_run: bool = False,
              tokenless_web: bool = False, language: str = ""):
    """main() from start to shutdown, over a config with one project, acme, whose repo app has `flow` as its
    ticket_flow and whose chat is 7 (in `language`, "": global.yaml's), and with the dashboard off. With
    `tokenless_web`, a second repo, web, on both but with no GitHub token. With `telegram`, a bot token and 42 as the
    global chat (no request is made). Returns what main built (the orchestrator, the runner, the keeper, the board,
    the intake, the replies and the command handler as "handler"; each only when built) and the names it gave
    _stop_background. A second call on the same tmp_path rewrites the config and keeps reviewer.db."""
    monkeypatch.setenv("ACME_GITHUB_TOKEN", "tok-app")
    cfg = tmp_path / "config-live"
    repos_dir = cfg / "projects" / "acme" / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    project_telegram = "telegram:\n  default_chat_id: '7'\n" + (f"  language: {language}\n" if language else "")
    (cfg / "projects" / "acme" / "project.yaml").write_text(
        "project:\n  id: acme\n  name: Acme\n" + project_telegram, encoding="utf-8")
    (repos_dir / "app.yaml").write_text(_repo_yaml("app", flow, "${ACME_GITHUB_TOKEN}"), encoding="utf-8")
    if tokenless_web:
        (repos_dir / "web.yaml").write_text(_repo_yaml("web", "both", ""), encoding="utf-8")
    global_yaml = _MINIMAL_GLOBAL_YAML.format(
        ws=tmp_path / "ws", log=tmp_path / "log", db=tmp_path / "events.db", rv=tmp_path / "rv" / "reviewer.db",
    ) + f"dashboard:\n  enabled: false\n  db_path: {tmp_path / 'events.db'}\n"
    if telegram:
        global_yaml = global_yaml.replace("bot_token: ''", "bot_token: '123:not-a-real-bot-token'", 1)
        global_yaml = global_yaml.replace("default_chat_id: ''", "default_chat_id: '42'", 1)
    (cfg / "global.yaml").write_text(global_yaml, encoding="utf-8")
    built: dict = {}

    def recorder(name, cls):
        def build(*args, **kwargs):
            built[name] = cls(*args, **kwargs)
            return built[name]
        return build

    async def no_event(self):
        return None

    async def no_send(self, chat_id, text, buttons=None, reply_to_message_id=None):
        return 1

    def keep_handler(self, handler):
        built["handler"] = handler

    stopped: list[str] = []

    async def stop_background(background, timeout):
        stopped.extend(background)
        await _stop_background(background, timeout)

    async def idle(self, stop):
        self.first_round.set()   # the first round is done at once: the runner's loop starts
        await stop.wait()

    async def no_tickets(self):
        built["orchestrator"] = self

    monkeypatch.setenv("GRADLE_USER_HOME", str(tmp_path / "gradle"))   # main caps build memory there
    monkeypatch.setattr(main, "_stop_background", stop_background)
    monkeypatch.setattr(runner_module, "SkillRunner", recorder("runner", FakeRunner))
    monkeypatch.setattr(keeper_module, "ComparisonKeeper", recorder("keeper", FakeRunner))
    monkeypatch.setattr(board_module, "ComparisonBoard", recorder("board", _Built))
    monkeypatch.setattr(intake_module, "SkillIntake", recorder("intake", _Built))
    monkeypatch.setattr(replies_module, "SkillReplies", recorder("replies", _Built))
    monkeypatch.setattr(TelegramAdapter, "start_polling", no_event)
    monkeypatch.setattr(TelegramAdapter, "stop_polling", no_event)
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


def test_main_wires_the_skill_flow(tmp_path, monkeypatch, capsys):
    cfg, built, stopped = _run_main(tmp_path, monkeypatch, language="uk")

    assert "  Skill flow: 1 repo(s)\n" in capsys.readouterr().out
    orch, runner, intake, replies = built["orchestrator"], built["runner"], built["intake"], built["replies"]
    repos, store, chat, llm = runner.args
    assert [r.scope for r in repos] == ["acme/app"]
    assert repos[0].checkout.path == tmp_path / "rv" / "skill" / "repos" / "acme" / "app"
    assert repos[0].vcs is orch._repo_vcs["app"][0]   # the staged flow's adapter for the repo
    assert type(llm).__name__ == "ClaudeCodeAdapter"
    # One store and one chat: a reply and the runner are kept apart by the lock inside the one store object.
    assert isinstance(store, SkillStore) and isinstance(chat, SkillChat)
    assert store is intake.args[0] is replies.args[0] is chat._store
    assert chat is replies.args[1] and isinstance(chat._notifier, TelegramAdapter)
    kwargs = runner.kwargs
    assert kwargs["slot"] is orch.build_slot is orch._runtime._build_semaphore
    assert kwargs["switches"] is orch._switches
    failing = kwargs["credential_failing"]
    assert isinstance(failing.__self__, CredentialHealth) and failing.__name__ == "failing"
    assert kwargs["get_mode"].__self__ is orch._mode_handler and kwargs["get_mode"].__name__ == "get_mode"
    assert kwargs["runs_dir"] == tmp_path / "rv" / "skill" / "runs"
    assert kwargs["secret_paths"] == [Path(main.__file__).parent / ".env", cfg.absolute()]
    assert kwargs["event_bus"] is orch._events is not None
    assert kwargs["language_of"]("acme/app") == "uk"   # the project's telegram.language
    # The comparisons: one store on the skill store's connection, on the orchestrator, the intake, the keeper, the
    # replies and the board.
    comparisons = orch._comparisons
    assert isinstance(comparisons, ComparisonStore) and comparisons._conn is store._conn
    assert intake.kwargs["comparisons"] is comparisons
    keeper, board = built["keeper"], built["board"]
    k_repos, k_comparisons, k_runs, k_chat, workspaces = keeper.args
    assert k_repos is repos and k_comparisons is comparisons and k_runs is store and k_chat is chat
    assert isinstance(workspaces, Workspaces) and workspaces._active == orch.get_active_workspaces
    tracker = MagicMock()
    orch.register_tracker("acme", tracker)
    assert keeper.kwargs["tracker_for"]("acme") is tracker   # by project id, as the keeper asks
    assert keeper.kwargs["event_bus"] is orch._events
    assert keeper.kwargs["language_of"]("acme/app") == "uk"
    ComparisonKeeper(*keeper.args, **keeper.kwargs)   # the real keeper takes what main gave it
    assert board.args == (comparisons, store, workspaces)
    # The intake: on the orchestrator, writing to the runs directory, waking the runner, telling the repo's chat.
    assert orch._skill_intake is intake
    _, runs_dir, chat_id_for = intake.args
    assert runs_dir == kwargs["runs_dir"] and set(intake.kwargs) == {"wake", "serves", "comparisons"}
    assert intake.kwargs["wake"] == runner.wake
    assert intake.kwargs["serves"]("acme/app") and not intake.kwargs["serves"]("acme/web")   # only the runner's repos
    assert chat_id_for(orch._projects["acme"].repos["app"]) == "7"   # the repo's chat
    assert chat_id_for(RepoConfig()) == "42"                           # else the global one
    # The replies: on the command handler, waking the runner and the keeper; a known scope's language, else the
    # chat's.
    assert replies.args[2] == runner.wake and built["handler"]._skill is replies
    assert replies.kwargs["comparisons"] is comparisons and replies.kwargs["wake_keeper"] == keeper.wake
    language_of = replies.kwargs["language_of"]
    assert language_of("acme/app", "42") == "uk"
    assert language_of("", "7") == "uk" and language_of("", "42") == "en"
    SkillReplies(*replies.args, **replies.kwargs)
    # The runner recovered at once, looped once the first credential round was done and stopped by its event; the
    # keeper, with nothing to recover, only looped.
    assert stopped == ["Credential health", "Skill runs", "Comparisons"]
    assert runner.started == 1 and [s.is_set() for s in runner.stops] == [True]
    assert keeper.started == 0 and [s.is_set() for s in keeper.stops] == [True]


def _workspace(tmp_path, repo_id: str):
    meta = tmp_path / "ws" / repo_id / "meta"
    meta.mkdir(parents=True)
    (meta / "ticket.md").write_text("# ACME-1")
    return SimpleNamespace(meta_dir=meta, state=SimpleNamespace(
        repo_id=repo_id, branch=f"feature/ACME-1-{repo_id}", model="sonnet", current_state=Stage.ANALYSIS,
        workspace_root=str(meta.parent), started_at=NOW))


def test_the_intake_main_builds_refuses_a_repo_it_skipped(tmp_path, monkeypatch, caplog):
    """web is on both but has no GitHub token: no runner serves it, so the intake makes no run for its tickets."""
    with caplog.at_level(logging.WARNING, logger="main"):
        _, built, _ = _run_main(tmp_path, monkeypatch, tokenless_web=True)
    assert "acme/web" in caplog.text and "no forge token" in caplog.text
    orch, recorded = built["orchestrator"], built["intake"]
    assert [r.scope for r in built["runner"].args[0]] == ["acme/app"]
    intake = SkillIntake(*recorded.args, **recorded.kwargs)   # the real intake, with what main gave it
    repos, store = orch._projects["acme"].repos, recorded.args[0]
    ticket = TicketData(id="ACME-1", url="https://acme.example/browse/ACME-1", summary="Login crashes",
                        description="")

    assert intake.create(_workspace(tmp_path, "web"), ticket, "acme", repos["web"]) is None
    assert store.runs_for_ticket("acme/web", "ACME-1") == []
    run = intake.create(_workspace(tmp_path, "app"), ticket, "acme", repos["app"])
    assert (run.scope, run.chat_id) == ("acme/app", "7")
    assert orch._comparisons.for_run(run.id) is not None   # and its comparison


def test_main_builds_nothing_without_a_both_repo(tmp_path, monkeypatch, capsys):
    _, built, stopped = _run_main(tmp_path, monkeypatch, flow="staged")

    assert "Skill flow" not in capsys.readouterr().out
    assert set(built) == {"orchestrator", "handler"}   # no runner, no intake, no replies
    assert getattr(built["orchestrator"], "_skill_intake", None) is None
    assert built["handler"]._skill is None
    assert stopped == ["Credential health"]


@pytest.mark.parametrize("telegram, dry_run, line", [(False, False, "  Skill flow: off (no Telegram)\n"),
                                                     (True, True, "  Skill flow: off (dry-run)\n")])
def test_main_skill_flow_off(tmp_path, monkeypatch, capsys, telegram, dry_run, line):
    """A run nobody could be told about, or one a dry run would make, is never created: no intake is attached."""
    _, built, stopped = _run_main(tmp_path, monkeypatch, telegram=telegram, dry_run=dry_run)

    out = capsys.readouterr().out
    assert line in out and "Skill flow: 1 repo(s)" not in out
    assert not {"runner", "intake", "replies"} & set(built)
    assert getattr(built["orchestrator"], "_skill_intake", None) is None
    if telegram:
        assert built["handler"]._skill is None
    assert stopped == ["Credential health"]


class MainRig:
    """main() run on one tmp_path, so every run reads and writes the same reviewer.db: `store` is a SkillStore on it,
    for what an earlier daemon left there."""

    def __init__(self, tmp_path, monkeypatch):
        self._tmp_path, self._monkeypatch = tmp_path, monkeypatch
        self.store = SkillStore(connect(tmp_path / "rv" / "reviewer.db"))
        self.stopped: list[str] = []
        self.language = ""   # acme's telegram.language for the next run

    def wired(self, both=()) -> SimpleNamespace:
        """main() with the repo app on ticket_flow both when it is in `both`, else staged; what it built."""
        _, built, self.stopped = _run_main(self._tmp_path, self._monkeypatch,
                                           flow="both" if "app" in both else "staged", language=self.language)
        return SimpleNamespace(orchestrator=built["orchestrator"], command_handler=built["handler"], built=built)

    def start(self) -> list[str]:
        """The names of the background tasks main stopped, with app on both."""
        self.wired(both=["app"])
        return self.stopped


@pytest.fixture
def main_rig(tmp_path, monkeypatch):
    return MainRig(tmp_path, monkeypatch)


def test_keeper_starts_and_stops_with_the_runner(main_rig):
    tasks = main_rig.start()
    assert {"Skill runs", "Comparisons"} <= set(tasks)


def test_comparisons_are_set_only_with_skill_repos(main_rig):
    assert getattr(main_rig.wired(both=[]).orchestrator, "_comparisons", None) is None
    assert main_rig.wired(both=["app"]).orchestrator._comparisons is not None


def test_skill_off_with_old_anchors(main_rig):
    main_rig.store.add_anchor("-100", 5, "run1", NOW)
    handler = main_rig.wired(both=[]).command_handler
    assert isinstance(handler._skill, SkillOff)
    # On the daemon's own store, answering in the chat's language.
    assert handler._skill.is_anchor("-100", 5) and not handler._skill.is_anchor("-100", 6)
    main_rig.language = "uk"
    off = main_rig.wired(both=[]).command_handler._skill
    assert off._language_of("7") == "uk" and off._language_of("42") == "en"   # acme's chat; the global one


def test_no_skill_off_on_a_daemon_that_never_used_it(main_rig):
    assert main_rig.wired(both=[]).command_handler._skill is None


# --- /status of the wired daemon ---

def test_status_counts_from_the_last_tracker_poll_not_from_the_start(main_rig, tmp_path):
    # Tickets on, as an earlier "/tickets on" left them: a first boot has them paused, and nothing is polled.
    Switches(connect(tmp_path / "rv" / "reviewer.db")).set("tickets", "running")
    wired = main_rig.wired(both=[])
    handler = wired.command_handler
    handler._last_poll_time -= 3600   # the daemon started an hour ago
    assert "Last Jira poll: 1h 0m ago" in handler._summary_text([])
    wired.orchestrator._trackers["acme"] = SimpleNamespace(poll_tickets=AsyncMock(return_value=[]))

    asyncio.run(wired.orchestrator._poll_and_create_workspaces())

    assert re.search(r"Last Jira poll: \ds ago", handler._summary_text([]))


# --- the code-review fix wave ---

@pytest.mark.parametrize("flow, telegram, opened", [
    ("staged", False, 2),   # the switches' and the reviewer store's: no skill connection
    ("both", False, 2),     # skill flow off (no Telegram): no skill repos
    ("staged", True, 3),    # the skill connection, for the has_anchors check
    ("both", True, 3),      # the skill connection, for the skill repos
])
def test_the_skill_connection_is_opened_only_where_it_is_used(tmp_path, monkeypatch, flow, telegram, opened):
    calls = []
    real = db_module.connect

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(db_module, "connect", counting)
    _run_main(tmp_path, monkeypatch, flow=flow, telegram=telegram)
    assert len(calls) == opened
