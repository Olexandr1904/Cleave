"""Tests for main's reviewer wiring: one reviewer context per loaded repo with reviews on, and
the background tasks (credential health, reviewer) started and stopped around the daemon."""
from __future__ import annotations

import asyncio
import logging
import time

import pytest
import respx

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
from main import _reviewer_contexts, _reviewer_send_line, _start_background, _stop_background
from orchestrator.reviewer.forge_github import ForgeError


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


async def test_reviewer_send_line_none_without_a_target():
    reviewer_chat = GlobalConfig(reviewer=ReviewerGlobalConfig(telegram_chat_id="42"))
    assert _reviewer_send_line(None, reviewer_chat, {}) is None   # no Telegram
    assert _reviewer_send_line(_Notifier(), GlobalConfig(), {}) is None   # no chat id
    notifier = _Notifier()
    await _reviewer_send_line(notifier, reviewer_chat, {})("line")
    assert notifier.sent == [("42", "line")]


async def test_reviewer_send_line_falls_back_to_the_operator_chat_and_raises_on_failure():
    project = LoadedProject(config=ProjectConfig(telegram=TelegramConfig(default_chat_id="7")))
    notifier = _Notifier()
    await _reviewer_send_line(notifier, GlobalConfig(), {"acme": project})("line")
    assert notifier.sent == [("7", "line")]
    broken = _reviewer_send_line(_Notifier(fail=RuntimeError("down")), GlobalConfig(), {"acme": project})
    with pytest.raises(RuntimeError):
        await broken("line")


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
