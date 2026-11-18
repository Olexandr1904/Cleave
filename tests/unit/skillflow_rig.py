# tests/unit/skillflow_rig.py — the skill runner's test rig, shared by the runner's and the publish steps' tests
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from config.schemas import RepoConfig, RepoInfo, SkillFlowConfig, TelegramConfig
from integrations.llm.llm_interface import LLMResponse
from orchestrator.reviewer.commands import redact
from orchestrator.reviewer.db import connect
from orchestrator.skillflow import publish as publish_module
from orchestrator.skillflow.chat import SkillChat
from orchestrator.skillflow.runner import SkillRepo, SkillRunner
from orchestrator.skillflow.store import SkillStore
from orchestrator.switches import Switches

T0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
NOW = T0.isoformat()
SCOPE = "acme/app"
CHAT_ID = "-100"
TOKEN = "ghp_not_a_real_token"     # the checkout's forge token, as its redact() masks it
PR_URL = "https://github.com/acme/app/pull/5"


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, **delta) -> None:
        self.t += timedelta(**delta)


class FakeCheckout:
    """The skill checkout with canned answers and a real directory under `path`. `calls` holds a method without a
    meaningful argument as its name and the others as (name, arg); prepare notes whether the slot was held, and
    `under_slot` names every other git call made while it was. `states` are commit_state's answers in order, the
    last one standing for every later check; `sha` is the worktree's HEAD; `forge_branches` are on the forge (none,
    so a run's own branch is never refused as someone else's); `pushed_shas` are the commits push was given."""
    base_branch = "develop"

    def __init__(self, path, slot):
        self.path, self.slot = path, slot
        self.calls, self.ticket = [], None
        self.prepare_error, self.missing, self.branches, self.changed = None, [], [], False
        self.slot_during_prepare, self.change_error = None, None
        self.states, self.files, self.push_errors, self.remove_answer = [], [], [], ""
        self.worktree_errors, self.on_remove, self.under_slot = {}, None, []
        self.sha, self.forge_branches, self.pushed_shas = "b" * 40, set(), []
        path.mkdir(parents=True)

    async def prepare(self):
        self.calls.append("prepare")
        self.slot_during_prepare = self.slot.locked()
        if self.prepare_error:
            raise self.prepare_error
        return "a" * 40

    def missing_files(self, required):
        self.calls.append("missing_files")
        return [rel for rel in required if rel in self.missing]

    def write_ticket(self, source, notes_rel):
        self.calls.append("write_ticket")
        self.ticket = notes_rel
        dest = self.path / notes_rel / "ticket"
        if dest.exists():                   # as the real one: an earlier copy is replaced
            shutil.rmtree(dest)
        shutil.copytree(source, dest)       # as the real one: a missing copy fails it
        return dest

    def redact(self, text):
        return redact(text, [TOKEN])

    async def branches_with(self, key):
        self.calls.append("branches_with")
        return list(self.branches)

    def worktree(self, rel):
        if rel in self.worktree_errors:
            raise self.worktree_errors[rel]
        return self.path / rel

    async def has_change(self, worktree):
        self.calls.append(("has_change", worktree))
        if self.change_error:
            raise self.change_error
        return self.changed

    async def commit_state(self, worktree):
        self._git("commit_state")
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def on_forge(self, branch):
        self._git(("on_forge", branch))
        return branch in self.forge_branches

    async def changed_files(self, worktree):
        self._git("changed_files")
        return list(self.files)

    async def head(self, worktree):
        self._git("head")
        return self.sha

    async def push(self, branch, sha):
        self._git(("push", branch))
        self.pushed_shas.append(sha)
        if self.push_errors:
            raise self.push_errors.pop(0)

    async def remove_worktree(self, worktree):
        self._git(("remove_worktree", worktree))
        if self.on_remove:
            self.on_remove()
        return self.remove_answer

    def _git(self, call):
        self.calls.append(call)
        if self.slot.locked():
            self.under_slot.append(call)


class FakeVcs:
    """The forge: `existing` maps a head branch to its open PR; open_pr records its arguments and opens PR 5."""

    def __init__(self):
        self.prs, self.error, self.keys = [], None, []
        self.existing, self.opened, self.finds = {}, [], []

    async def open_prs_for_key(self, key):
        self.keys.append(key)
        if self.error:
            raise self.error
        return list(self.prs)

    async def find_pr_by_branch(self, branch):
        self.finds.append(branch)
        return self.existing.get(branch)

    async def open_pr(self, title, body, head_branch, base_branch, draft=False):
        self.opened.append((title, body, head_branch, base_branch, draft))
        self.existing[head_branch] = (5, PR_URL)
        return self.existing[head_branch]


class FakeLLM:
    """execute_in_workspace records its keyword arguments and answers the next text as a response, or raises the
    next exception; on_call runs inside the call, after_call[i] once call i has its answer."""

    def __init__(self, items):
        self.items, self.calls, self.on_call, self.after_call = list(items), [], None, {}

    async def execute_in_workspace(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call:
            self.on_call()
        item = self.items.pop(0)
        if hook := self.after_call.get(len(self.calls) - 1):
            hook()
        if isinstance(item, BaseException):
            raise item
        return LLMResponse(item, input_tokens=1000, output_tokens=200, cost_usd=0.5, num_turns=3)


@dataclass
class Sent:
    chat_id: str
    text: str
    buttons: list | None
    message_id: int


class FakeNotifier:
    """Records each message and returns 500, 501, …; with `fail` set a send raises; `during` runs inside every
    send, before it fails or goes out."""

    def __init__(self):
        self.sent, self.fail, self.during = [], False, None

    async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
        if self.during:
            self.during()
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(Sent(chat_id, text, buttons, 500 + len(self.sent)))
        return self.sent[-1].message_id

    async def delete_message(self, chat_id, message_id):
        pass


class Rig:
    """A runner on a real store, switches, chat, prompts, status reader and gates; a fake checkout, forge, LLM
    and notifier; a clock that moves only when told. One queued run for ACME-1, its ticket copy on disk, and its
    worktree a real directory the gate commands run in."""

    def __init__(self, tmp_path, mode="auto", texts=(), idle_seconds=0.01, gates=None, gate_rounds=2,
                 language_of=None):
        conn = connect(tmp_path / "reviewer.db")
        self.store, self.switches = SkillStore(conn), Switches(conn)
        self.switches.set("tickets", "running")          # paused on a first boot
        self.clock, self.mode, self.failing, self.events, self.seen = Clock(T0), mode, set(), [], []
        self.slot = asyncio.Semaphore(1)
        self.checkout = FakeCheckout(tmp_path / "skill" / "app", self.slot)
        self.vcs, self.llm = FakeVcs(), FakeLLM(texts)
        self.worktree = self.checkout.path / "worktrees" / "ACME-1"
        self.worktree.mkdir(parents=True)
        cfg = RepoConfig(repo=RepoInfo(id="app"), ticket_flow="both", telegram=TelegramConfig(default_chat_id=CHAT_ID),
                         skill_flow=SkillFlowConfig(
                             bug_skill="fix-this", task_skill="implement-this", notes_dir="ai_instructions/{key}",
                             worktree_dir="worktrees/{key}", required_files=["local.properties"],
                             build_env=["JAVA_HOME"], effort="xhigh", gates=dict(gates or {}),
                             gate_rounds=gate_rounds))
        self.repo = SkillRepo("acme", "app", cfg, self.checkout, self.vcs)
        self.notifier = FakeNotifier()

        async def no_sleep(seconds):
            pass

        self.chat = SkillChat(self.notifier, self.store, now=self.clock.now, sleep=no_sleep)
        self.environ = {"HOME": "/home/octo", "PATH": "/usr/bin:/bin", "JAVA_HOME": "/jdk", "GITHUB_TOKEN": "t"}
        self.runs_dir = tmp_path / "runs"
        self.runner = SkillRunner(
            [self.repo], self.store, self.chat, self.llm, slot=self.slot, switches=self.switches,
            credential_failing=self.failing.__contains__, get_mode=lambda: self.mode, runs_dir=self.runs_dir,
            secret_paths=[tmp_path / ".env"], event_bus=self, now=self.clock.now, environ=self.environ,
            idle_seconds=idle_seconds, **({} if language_of is None else {"language_of": language_of}))
        self.add_run("ACME-1", NOW, summary="Login crashes",
                     facts={"assignee": "Ann", "status": "To Do", "staged_branch": "feature/ACME-1-acme-1"})
        self.gate_seen = []

    def add_run(self, key, created, summary="", facts=None):
        """A queued run of `key` created at `created`, its ticket copy on disk."""
        run = self.store.add_run(SCOPE, key, created, skill="fix-this", summary=summary, model="claude-opus-5-5",
                                 chat_id=CHAT_ID, facts=facts or {})
        (self.runs_dir / run.id / "ticket").mkdir(parents=True)
        (self.runs_dir / run.id / "ticket" / "ticket.md").write_text(f"# {key} {summary}")
        return run

    def emit(self, event_type, message, *, project_id=None, ticket_id=None, data=None):
        self.events.append(SimpleNamespace(type=event_type, message=message, project_id=project_id,
                                           ticket_id=ticket_id, data=data))

    async def go(self):
        run = self.runner.next_run()
        assert run is not None, "no run to take"
        await self.runner.run_job(run)
        return self.store.run(run.id)

    def block(self, gate):
        if gate == "tickets_paused":
            self.switches.set("tickets", "paused", scope=SCOPE)
        else:
            self.failing.add(gate)

    def unblock(self, gate):
        if gate == "tickets_paused":
            self.switches.set("tickets", "running", scope=SCOPE)
        else:
            self.failing.discard(gate)

    def write_description(self, text, key="ACME-1"):
        """The PR description the session writes in the main checkout's notes directory."""
        notes = self.checkout.path / "ai_instructions" / key
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "PR_DESCRIPTION.md").write_text(text)

    def spy_gates(self, monkeypatch):
        """While the gates run, record whether the slot is held and the run's stored state."""
        real = publish_module.run_gates

        async def spy(worktree, commands, env, **kwargs):
            [run] = self.store.runs_for_ticket(SCOPE, "ACME-1")
            self.gate_seen.append((self.slot.locked(), run.state))
            return await real(worktree, commands, env, **kwargs)

        monkeypatch.setattr(publish_module, "run_gates", spy)
