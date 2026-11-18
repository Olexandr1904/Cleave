# tests/unit/test_skillflow_keeper.py
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from config.schemas import RepoConfig, RepoInfo, TelegramConfig
from integrations.github.github_adapter import PRInfo
from orchestrator.reviewer.commands import redact
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.messages import MESSAGE_LIMIT
from orchestrator.skillflow.chat import SendError, SkillChat
from orchestrator.skillflow.compare_store import ComparisonStore
from orchestrator.skillflow.keeper import ComparisonKeeper
from orchestrator.skillflow.runner import SkillRepo
from orchestrator.skillflow.sides import Workspaces, pair_fingerprint, skill_view, staged_view
from orchestrator.skillflow.store import SkillStore
from workspace.workspace import Workspace, WorkspaceState

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
NOW = T0.isoformat()
SCOPE = "acme/app"
CHAT_ID = "-100"
TOKEN = "ghp_not_a_real_token"


class FakeForge:
    """The repo's GitHubAdapter: `prs` maps a PR number to its state ("open" | "closed"), titles end in their
    flow's mark, URLs are u<number>. `calls` records every call; fail(name, error) makes that call raise until
    heal()."""

    def __init__(self):
        self.prs = {12: "open", 13: "open"}
        self.titles = {12: "ACME-1: Fix login [staged]", 13: "ACME-1: Fix login [skill]"}
        self.calls, self.failing = [], {}

    def fail(self, name, error):
        self.failing[name] = error

    def heal(self):
        self.failing.clear()

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.failing:
            raise self.failing[name]

    async def pr_info(self, n):
        self._call("pr_info", n)
        return PRInfo(number=n, title=self.titles[n], state=self.prs[n], draft=True, node_id=f"N{n}", url=f"u{n}")

    async def mark_ready(self, n):
        self._call("mark_ready", n)

    async def set_title(self, n, title):
        self._call("set_title", n, title)
        self.titles[n] = title

    async def comment_on_pr(self, n, body):
        self._call("comment_on_pr", n, body)

    async def close_pr(self, n):
        self._call("close_pr", n)
        self.prs[n] = "closed"


class FakeTracker:
    def __init__(self):
        self.calls = []

    async def transition_ticket(self, ticket_id, status):
        self.calls.append(("transition", ticket_id, status))

    async def add_comment(self, ticket_id, text):
        self.calls.append(("comment", ticket_id, text))


class FakeChat:
    """SkillChat.send: records (text, buttons) and returns one message id per send, from 700; `down` makes a
    send raise SendError with no part out."""

    def __init__(self):
        self.sent, self.ids, self.down = [], [], False

    async def send(self, target, text, buttons=None, skip=0):
        if self.down:
            raise SendError("not sent: RuntimeError")
        self.sent.append((text, buttons))
        self.ids.append(700 + len(self.ids))
        return self.ids[-1:]


class Staged:
    """The staged side: setting `state` puts the workspace in that state on disk; reading it gives its
    WorkspaceState."""

    def __init__(self, ws):
        self.ws = ws

    @property
    def state(self):
        return self.ws.state

    @state.setter
    def state(self, value):
        self.ws.state.current_state = value
        self.ws.save_state()


class Rig:
    """A keeper on one served scope acme/app with real stores: a staged workspace in PR_REVIEW with PR #12, a
    skill run pr_open with PR #13, their comparison open."""

    def __init__(self, tmp_path):
        conn = connect(tmp_path / "reviewer.db")
        self.comparisons, self.runs = ComparisonStore(conn), SkillStore(conn)
        self.forge, self.tracker, self.chat, self.events = FakeForge(), FakeTracker(), FakeChat(), []
        run = self.runs.add_run(SCOPE, "ACME-1", NOW, skill="fix-this", summary="Fix login", chat_id=CHAT_ID)
        self.runs.claim(run.id, ("queued",), NOW, "pr_open", pr_number=13, pr_url="u13", turn_count=4)
        self.run_id = run.id
        root = tmp_path / "ws"
        root.mkdir()
        ws = Workspace(str(root), WorkspaceState(ticket_id="ACME-1", company_id="acme", repo_id="app",
                                                 workspace_root=str(root), current_state="PR_REVIEW",
                                                 started_at=NOW, pr_number=12, pr_url="u12"))
        ws.save_state()
        self.staged, self.active = Staged(ws), [ws]
        self.workspaces = Workspaces(lambda: list(self.active))
        cfg = RepoConfig(repo=RepoInfo(id="app"), ticket_flow="both", telegram=TelegramConfig(default_chat_id=CHAT_ID))
        checkout = SimpleNamespace(redact=lambda text: redact(text, [TOKEN]))
        self.repo = SkillRepo("acme", "app", cfg, checkout, self.forge)
        self.cid = self.comparisons.add(SCOPE, "ACME-1", run.id, NOW, chat_id=CHAT_ID, summary="Fix login",
                                        skill="fix-this", workspace_root=str(root), workspace_started_at=NOW).id
        self.keeper = self.new_keeper()

    def new_keeper(self, **kwargs):
        return ComparisonKeeper([self.repo], self.comparisons, self.runs, self.chat, self.workspaces,
                                # As the orchestrator's: trackers are keyed by project id, not scope.
                                tracker_for=lambda project_id: self.tracker if project_id == "acme" else None,
                                language_of=lambda scope: "en",
                                event_bus=self, now=lambda: T0, **kwargs)

    def emit(self, event_type, message, *, project_id=None, ticket_id=None, data=None):
        self.events.append(SimpleNamespace(type=event_type, message=message, project_id=project_id,
                                           ticket_id=ticket_id, data=data))

    def views(self):
        return (skill_view(self.runs.run(self.run_id), "", T0),
                staged_view(self.workspaces.find(self.comparisons.get(self.cid)), "", T0))

    def fingerprint(self):
        return pair_fingerprint(*self.views())

    def sides(self):
        skill, staged = self.views()
        return {"skill": skill.as_dict(), "staged": staged.as_dict()}

    def delete_workspace(self):
        shutil.rmtree(self.staged.ws.root)
        self.active.clear()

    async def message_out(self):
        await self.keeper.cycle()
        assert len(self.chat.sent) == 1, "the comparison message did not go out"

    async def decided(self, verdict):
        """The message out, the tap's claim of the verdict, and the keeper's next cycle."""
        await self.message_out()
        self.comparisons.claim(self.cid, ("open",), NOW, "deciding", verdict=verdict, decided_at=NOW)
        await self.keeper.cycle()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# --- the brief's tests ---

async def test_message_once_both_rest(rig):
    rig.staged.state = "ANALYSIS"
    await rig.keeper.cycle()
    assert rig.chat.sent == []
    rig.staged.state = "PR_REVIEW"
    await rig.keeper.cycle(); await rig.keeper.cycle()
    (text, buttons), = rig.chat.sent
    assert "ACME-1 · comparison: Fix login" in text
    assert [b.action for b in buttons] == ["sk_win_staged:" + rig.cid, "sk_win_skill:" + rig.cid, "sk_win_none:" + rig.cid]
    assert rig.comparisons.get(rig.cid).ask_message_ids == rig.chat.ids[-1:]

async def test_a_side_that_moves_on_reopens(rig):
    await rig.keeper.cycle()
    rig.runs.claim(rig.run_id, ("pr_open",), NOW, "running")        # as if it moved on
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert c.fingerprint == "" and c.ask_message_ids == []
    rig.runs.claim(rig.run_id, ("running",), NOW, "pr_open", turn_count=9)
    await rig.keeper.cycle()
    assert len(rig.chat.sent) == 2 and rig.comparisons.get(rig.cid).messages == 2

async def test_moved_on_before_the_first_step_undoes_the_verdict(rig):
    await rig.message_out()
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="skill")
    rig.runs.claim(rig.run_id, ("pr_open",), NOW, "pr_open", turn_count=99)
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert c.state == "open" and c.verdict == "" and rig.forge.calls == []
    assert "moved on after your verdict" in rig.chat.sent[-1][0]

async def test_a_restart_continues_a_deciding_comparison(rig):
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="skill", steps_done=["ready", "title"],
                          fingerprint=rig.fingerprint(), sides=rig.sides())
    keeper = rig.new_keeper()
    await keeper.cycle()
    assert ("mark_ready", 13) not in rig.forge.calls and rig.comparisons.get(rig.cid).state == "closed"

async def test_telegram_down_keeps_the_message_for_later(rig):
    rig.chat.down = True
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).unsent == 1
    rig.chat.down = False
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).unsent == 0 and len(rig.chat.sent) == 1

async def test_an_unserved_scope_is_left_alone(rig):
    other = rig.comparisons.add("acme/web", "ACME-2", "run9", NOW)
    await rig.keeper.cycle()
    assert rig.comparisons.get(other.id).fingerprint == ""


# --- beyond the brief's list ---

async def test_rest_since_is_kept_while_a_side_rests_and_dropped_when_it_moves(rig):
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).rest_since == {"skill": NOW, "staged": NOW}
    rig.staged.state = "ANALYSIS"
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).rest_since == {"skill": NOW}

async def test_a_deciding_comparison_of_an_unserved_scope_waits(rig):
    other = rig.comparisons.add("acme/web", "ACME-2", "run9", NOW)
    rig.comparisons.claim(other.id, ("open",), NOW, "deciding", verdict="neither")
    await rig.keeper.cycle()
    c = rig.comparisons.get(other.id)
    assert (c.state, c.steps_done) == ("deciding", [])

async def test_a_comparison_that_raises_holds_back_only_itself(rig, caplog):
    broken = rig.comparisons.add(SCOPE, "ACME-2", "run9", NOW)
    real = rig.workspaces.find

    def find(c):
        if c.id == broken.id:
            raise RuntimeError("unreadable")
        return real(c)

    rig.workspaces.find = find
    with caplog.at_level(logging.WARNING):
        await rig.keeper.cycle()
    assert len(rig.chat.sent) == 1 and "ACME-1" in rig.chat.sent[0][0]
    assert [r for r in caplog.records if broken.id in r.getMessage()] != []

async def test_the_loop_survives_a_failing_cycle_and_stops(rig):
    calls = []

    async def cycle():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop.set()

    stop = asyncio.Event()
    keeper = rig.new_keeper(idle_seconds=0.01)
    keeper.cycle = cycle
    await asyncio.wait_for(keeper.loop(stop), timeout=2)
    assert len(calls) == 2


# --- the final fix wave ---

async def test_a_state_that_is_no_object_reads_the_staged_side_as_gone(rig, caplog):
    rig.active.clear()
    rig.staged.ws.state_path.write_text(json.dumps([1, 2]))
    with caplog.at_level(logging.WARNING):
        await rig.keeper.cycle()
    (text, _), = rig.chat.sent
    assert "Staged: no PR · gone" in text and "AttributeError" in caplog.text

async def test_a_deciding_comparison_whose_workspace_turned_unreadable_is_undone(rig):
    await rig.message_out()
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="skill", decided_at=NOW)
    rig.active.clear()
    rig.staged.ws.state_path.write_text(json.dumps([1, 2]))
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).state == "open" and rig.forge.calls == []
    assert "the staged side moved on after your verdict" in rig.chat.sent[-1][0]

async def test_a_deciding_comparison_whose_sides_cannot_be_read_is_stuck(rig):
    await rig.message_out()
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="skill", decided_at=NOW,
                          rest_since={"skill": "not a time", "staged": NOW})
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert (c.state, c.reason, c.steps_done) == ("stuck", "ValueError", [])
    text, buttons = rig.chat.sent[-1]
    assert "failed: ValueError" in text and [b.action for b in buttons] == ["sk_retry:" + rig.cid]
    assert rig.forge.calls == []

async def test_a_message_still_queued_when_a_side_moves_on_is_not_sent(rig):
    rig.chat.down = True
    await rig.keeper.cycle()
    rig.staged.state = "DEV"                       # the staged side moved on (a dashboard retry, say)
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert (c.fingerprint, c.outbox, c.ask_message_ids) == ("", [], [])
    rig.chat.down = False
    await rig.keeper.cycle()
    assert rig.chat.sent == []

async def test_a_half_sent_message_is_dropped_when_a_side_moves_on(rig):
    rig.chat.down = True
    await rig.keeper.cycle()
    rig.comparisons.mark_parts(rig.cid, [650], NOW)    # its first part went out before Telegram went down
    rig.runs.claim(rig.run_id, ("pr_open",), NOW, "running")
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert (c.outbox, c.ask_message_ids) == ([], [])

async def test_a_new_message_supersedes_one_still_queued(rig):
    rig.chat.down = True
    await rig.keeper.cycle()
    rig.runs.claim(rig.run_id, ("pr_open",), NOW, "pr_open", turn_count=9)   # changed, still at rest
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert c.messages == 2 and len(c.outbox) == 1
    rig.chat.down = False
    await rig.keeper.cycle()
    assert len(rig.chat.sent) == 1 and rig.comparisons.get(rig.cid).ask_message_ids == rig.chat.ids


class Notifier:
    """The notifier under a real SkillChat: send_message returns ids from 901; `fail_on` (a 1-based send count)
    raises once."""

    def __init__(self):
        self.sent, self.fail_on, self.count = [], None, 0

    async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
        self.count += 1
        if self.fail_on == self.count:
            raise RuntimeError("telegram 502")
        self.sent.append((text, buttons))
        return 900 + len(self.sent)


async def test_a_partial_send_is_not_sent_again_and_every_part_takes_a_tap(rig):
    notifier = Notifier()
    rig.chat = SkillChat(notifier, rig.runs, now=lambda: T0)
    rig.keeper = rig.new_keeper()
    rig.comparisons.claim(rig.cid, ("open",), NOW, "open", summary="word " * (MESSAGE_LIMIT // 4))   # two parts
    notifier.fail_on = 2
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert (c.unsent, c.outbox[0]["ids"], c.ask_message_ids) == (1, [901], [])
    await rig.keeper.cycle()
    c = rig.comparisons.get(rig.cid)
    assert c.unsent == 0 and len(notifier.sent) == 2 and c.ask_message_ids == [901, 902]
    assert [bool(buttons) for _, buttons in notifier.sent] == [False, True]
    assert all(rig.runs.anchor(CHAT_ID, mid) == rig.cid for mid in c.ask_message_ids)


# --- the code-review fix wave ---

@pytest.mark.parametrize("verdict, named", [("skill", "marking PR #13 ready failed: ValueError"),
                                            ("staged", "marking PR #12 ready failed: ValueError"),
                                            ("neither", "tracker writes failed: ValueError")])
async def test_a_verdict_stuck_on_sides_it_cannot_read_names_its_step_and_pr(rig, verdict, named):
    await rig.message_out()
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict=verdict, decided_at=NOW,
                          rest_since={"skill": "not a time", "staged": NOW})
    await rig.keeper.cycle()
    text, _ = rig.chat.sent[-1]
    assert rig.comparisons.get(rig.cid).state == "stuck"
    assert named in text and "PR #0" not in text


async def test_a_verdict_whose_stored_sides_are_of_a_wrong_shape_is_stuck_all_the_same(rig):
    await rig.message_out()
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict="skill", decided_at=NOW, sides="a text",
                          rest_since={"skill": "not a time", "staged": NOW})
    await rig.keeper.cycle()
    assert rig.comparisons.get(rig.cid).state == "stuck" and "marking PR #0 ready failed" in rig.chat.sent[-1][0]


@pytest.mark.parametrize("verdict, steps_done, named", [
    ("staged", ["ready", "title"], "commenting on PR #13 failed: RuntimeError"),   # the loser's PR
    ("skill", [], "marking PR #13 ready failed: RuntimeError"),
    ("neither", ["ready", "title", "comment", "close", "tracker"], "ending the skill run failed: RuntimeError"),
    ("neither", [], "tracker writes failed: RuntimeError")])
async def test_a_verdict_not_started_names_its_next_step_and_pr(rig, verdict, steps_done, named):
    await rig.message_out()
    rig.comparisons.claim(rig.cid, ("open",), NOW, "deciding", verdict=verdict, decided_at=NOW,
                          steps_done=steps_done)

    def no_tracker(project_id):
        raise RuntimeError("tracker config")

    keeper = ComparisonKeeper([rig.repo], rig.comparisons, rig.runs, rig.chat, rig.workspaces,
                              tracker_for=no_tracker, language_of=lambda scope: "en", now=lambda: T0)
    await keeper.cycle()
    text, _ = rig.chat.sent[-1]
    assert rig.comparisons.get(rig.cid).state == "stuck"
    assert named in text and "PR #0" not in text
