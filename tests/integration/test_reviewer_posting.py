# tests/integration/test_reviewer_posting.py
"""The posting smoke run: a reply to a review message ("пости 1 2 5 апрув") comes in through the Telegram adapter
as the daemon wires it (the allowlist from main) and goes through the real command handler, the reviewer's replies
and Posting, with reviews paused. The repo's poster tool (a fake script) runs a dry run and a confirm run from the
real review clone with the forge token; the real GitHub reader (respx, reads only) shows the new review, and the
operator gets the "Posted" line. The same reply again is refused: nothing runs twice. With no allowed chat, the
reviewer reads no reply at all."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import respx

from config.schemas import (
    GlobalConfig,
    PosterPartConfig,
    ReadinessPartConfig,
    ReviewerConfig,
    ReviewerGlobalConfig,
    TelegramConfig,
)
from integrations.github.github_adapter import GitHubAdapter
from integrations.telegram.command_handler import CommandHandler
from integrations.telegram.handlers.mode import ModeHandler
from integrations.telegram.telegram_adapter import TelegramAdapter
from main import _allowed_chat_ids
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.clone import ReviewClone
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import GitHubForgeReader
from orchestrator.reviewer.models import JobRun, RepoState
from orchestrator.reviewer.poster import CommandPoster
from orchestrator.reviewer.posting import Posting
from orchestrator.reviewer.readiness import CommandReadiness
from orchestrator.reviewer.replies import ReviewerReplies
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches
from tests.unit.test_reviewer_poster_output import DRY
from tests.unit.test_reviewer_report import HEAD, REPORT

TOKEN = "ghp_smoke_not_real"
CHAT = "-1001"
SCOPE = "acme/app"
N = 1712
REPO = "/repos/acme-org/acme-app"
API = f"https://api.github.com{REPO}"
PR_URL = "https://github.com/acme-org/acme-app/pull/1712"
REVIEW_URL = f"{PR_URL}#pullrequestreview-11"
NAME = f"report_{HEAD[:9]}.md"
STARTED = "2026-11-20T10:00:00+00:00"
PR = {"number": N, "title": "Fix cart sync", "body": "Keeps the cart in sync.", "html_url": PR_URL,
      "user": {"login": "octo-author"}, "state": "open", "head": {"sha": HEAD}, "base": {"ref": "develop"},
      "draft": False, "assignees": [], "requested_reviewers": [], "changed_files": 14, "additions": 20,
      "deletions": 10, "updated_at": "2026-11-20T09:00:00Z", "closed_at": None}
# What the confirm run of DRY lands: one APPROVED review at HEAD with the payload's body and its two comments.
NEW_REVIEW = {"id": 11, "user": {"login": "octo"}, "state": "APPROVED", "commit_id": HEAD,
              "submitted_at": "2026-11-20T12:01:00Z", "html_url": REVIEW_URL,
              "body": json.loads(DRY.split("--- DRY RUN batch 1/1 ---\n")[1])["body"]}
COMMENTS = [{"path": "app/src/main/CartRepository.kt", "line": 88,
             "body": "**NPE on an empty list**\n\nThe list is read before the null check."},
            {"path": "app/src/main/SyncWorker.kt", "line": 43,
             "body": "**Retry without backoff**\n\nNetwork retry rule.\n\n_Anchor moved from line 41._"}]
POSTED = (f"Posted on #1712 at 26c4929c5 — APPROVE, 1 review: {REVIEW_URL}\n"
          "inline: 1 2 · moved anchor: 2 · in the review body: 5")
IGNORED = "  Reviewer: replies are ignored — reviewer.telegram_chat_id is not an allowed chat\n"
# The repo's poster tool: logs its argv and whether GH_TOKEN is set (never its value), then prints the dry run or
# the posted batch.
FAKE_POSTER = """import json, os, sys
with open(@LOG@, "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "gh_token": bool(os.environ.get("GH_TOKEN"))}) + "\\n")
if sys.argv[-1] == "--dry-run":
    sys.stdout.write(@DRY@)
elif sys.argv[-1] == "--yes":
    print("Posted batch 1/1: " + @URL@)
else:
    sys.exit(2)
"""


class Notifier:
    def __init__(self):
        self.sent: list[tuple[str, str, int | None]] = []

    async def send_message(self, chat_id, text, buttons=None, reply_to_message_id=None):
        self.sent.append((chat_id, text, reply_to_message_id))
        return 500 + len(self.sent)  # the Telegram message id


class Daemon:
    """The reply path as main wires it, around a done review of PR 1712 at HEAD (plan 4's report) whose copy holds
    the report and anchors.json, anchored at (CHAT, 100); reviewer.telegram_chat_id is CHAT, telegram.default_chat_id
    `default_chat`. The fake tool, a real CommandPoster, ReviewClone over an empty dir, GitHubForgeReader, Posting,
    ReviewerService (reviews paused), ReviewerReplies, CommandHandler and TelegramAdapter. What falls through to
    handle_message (the ticket intent parser's path) is recorded, never run."""

    def __init__(self, tmp_path, default_chat: str):
        self.data = tmp_path / "rv"  # the reviewer data dir
        db = self.data / "reviewer.db"
        self.store = ReviewerStore(connect(db))
        self.switches = Switches(connect(db))
        self.switches.set("reviews", "paused", by="test")  # posting works while reviews are paused
        self.store.save_repo_state(RepoState(SCOPE, first_poll_done=True, me="octo"))
        self.job = self.store.add_job(SCOPE, N, "review", HEAD, HEAD, STARTED)
        self.store.set_job_state(self.job.id, "done", "", STARTED)
        self.job_dir = self.data / "jobs" / self.job.id
        self.job_dir.mkdir(mode=0o700, parents=True)
        (self.job_dir / NAME).write_text(REPORT, encoding="utf-8")
        (self.job_dir / "anchors.json").write_text(json.dumps({"pr": str(N)}), encoding="utf-8")
        self.store.save_run(JobRun(self.job.id, started_at=STARTED, job_dir=str(self.job_dir), url=PR_URL,
                                   report=parse_report(REPORT, HEAD).to_dict()))
        self.store.add_anchor(CHAT, 100, self.job.id, STARTED)
        poster_cfg = self._fake_poster(tmp_path)
        clone_dir = self.data / "repos" / "acme" / "app"
        clone_dir.mkdir(parents=True)  # an empty clone: the tool runs from it
        self.github = GitHubAdapter(TOKEN, "acme-org", "acme-app")
        ctx = RepoContext(
            "acme", "app", ReviewerConfig(enabled=True, base_branch="develop", poster=poster_cfg),
            clone=ReviewClone(clone_dir, "https://github.com/acme-org/acme-app.git", "develop", TOKEN),
            readiness=CommandReadiness(ReadinessPartConfig(), repo="acme-org/acme-app", token=TOKEN),
            forge=GitHubForgeReader(self.github),
            poster=CommandPoster(poster_cfg, repo="acme-org/acme-app", token=TOKEN),
            allowed_links=("https://github.com/acme-org/",),
        )
        self.notifier = Notifier()
        chat = ReviewerChat(self.notifier, CHAT, store=self.store)
        service = ReviewerService([ctx], self.store, self.switches, lambda key: False, chat.send,
                                  lambda project, repo: set())
        self.replies = ReviewerReplies([ctx], self.store, service, chat,
                                       Posting(self.store, ledgers_dir=self.data / "ledgers"), None, None,
                                       credential_failing=lambda key: False)
        # As main builds them: the allowlist (an empty one locks the bot) and the reviewer's chats.
        config = GlobalConfig(telegram=TelegramConfig(default_chat_id=default_chat),
                              reviewer=ReviewerGlobalConfig(telegram_chat_id=CHAT))
        allowed = _allowed_chat_ids(config, {})
        self.parsed: list[str] = []
        parsed = self.parsed

        class IntentParser:   # the ticket pipeline's parser: no reviewer reply may reach it
            async def parse(self, text, *args, **kwargs):
                parsed.append(text)

        self.handler = CommandHandler(
            intent_parser=IntentParser(), notifier=self.notifier,
            mode_handler=ModeHandler(state_file_path=str(tmp_path / "daemon_state.json")),
            active_workspaces_fn=list, allowed_chat_ids=allowed, reviewer_chat_ids=allowed,
            switches=self.switches, reviewer=self.replies,
        )
        self.fell_through: list[tuple[str, str]] = []
        ticket_path = self.handler.handle_message

        async def handle_message(text, chat_id):
            self.fell_through.append((text, chat_id))
            await ticket_path(text, chat_id)

        self.handler.handle_message = handle_message
        with patch("integrations.telegram.telegram_adapter.Bot"):
            self.telegram = TelegramAdapter(bot_token="123:not-a-real-bot-token")
        self.telegram.set_command_handler(self.handler)

    def _fake_poster(self, tmp_path) -> PosterPartConfig:
        tools = tmp_path / "tools"
        tools.mkdir()
        self.log = tmp_path / "poster.log"
        script = tools / "post.py"
        script.write_text(FAKE_POSTER.replace("@LOG@", repr(str(self.log))).replace("@DRY@", repr(DRY))
                          .replace("@URL@", repr(REVIEW_URL)), encoding="utf-8")
        return PosterPartConfig(review_command=[
            sys.executable, str(script), "{pr}", "--report", "{report}", "--anchors", "{job_dir}/anchors.json",
            "--ledger", "{ledger}", "--event", "{event}"])

    async def reply(self, text: str) -> None:
        """A reply in CHAT to message 100, sent now, as Telegram delivers it; then the posts it started."""
        update = MagicMock()
        update.message.text = text
        update.message.reply_to_message.message_id = 100
        update.message.chat.id = int(CHAT)
        update.message.date = datetime.now(timezone.utc)  # the reply's age: Cleave reads it
        await self.telegram._handle_incoming(update, None)
        await self.replies.wait_idle()

    def runs(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def ticked(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"^### \[x\] (\d+)\.", text, re.MULTILINE)]


@respx.mock  # assert_all_mocked: a request outside these routes fails
async def test_post_end_to_end(tmp_path, capsys):
    daemon = Daemon(tmp_path, default_chat=CHAT)
    assert capsys.readouterr().out == ""  # the reviewer chat is an allowed chat
    respx.get(f"{API}/pulls/{N}").respond(200, json=PR)
    # Before the dry run, right before the confirm run, then the post-condition: a fourth read would fail.
    respx.get(f"{API}/pulls/{N}/reviews").mock(side_effect=[
        httpx.Response(200, json=[]), httpx.Response(200, json=[]), httpx.Response(200, json=[NEW_REVIEW])])
    respx.get(f"{API}/pulls/{N}/reviews/11/comments").respond(200, json=COMMENTS)
    try:
        await daemon.reply("пости 1 2 5 апрув")

        # The tool ran twice, a dry run then the confirm run: one argv, only the flag swapped, the token set.
        ledger = daemon.data / "ledgers" / "acme" / "app" / "PR_1712" / "posted_26c4929c5.json"
        argv = [str(N), "--report", str(daemon.job_dir / NAME), "--anchors", str(daemon.job_dir / "anchors.json"),
                "--ledger", str(ledger), "--event", "APPROVE"]
        runs = daemon.runs()
        assert [run["argv"] for run in runs] == [[*argv, "--dry-run"], [*argv, "--yes"]]
        assert [run["gh_token"] for run in runs] == [True, True]
        assert ticked((daemon.job_dir / NAME).read_text(encoding="utf-8")) == [1, 2, 5]  # exactly the named
        assert daemon.store.job(daemon.job.id).state == "posted"
        assert [text for _, text, _ in daemon.notifier.sent] == [
            "Checking before posting: post 1 2 5 + APPROVE at 26c4929c5.", POSTED]
        assert all(chat_id == CHAT and reply_to == 100 for chat_id, _, reply_to in daemon.notifier.sent)

        # The same reply again (a redelivery, or sent twice): refused, the tool not run a third time.
        await daemon.reply("пости 1 2 5 апрув")
        assert daemon.notifier.sent[-1] == (CHAT, "#1 is already posted at this head. Nothing was done.", 100)
        assert len(daemon.runs()) == 2
    finally:
        await daemon.github._client.aclose()

    assert daemon.fell_through == []  # never the ticket pipeline's handle_message, so never its intent parser
    # GitHub was only read, in the posting order: no POST, nothing outside the routes.
    reads = [(call.request.method, call.request.url.path) for call in respx.calls]
    assert reads == [("GET", f"{REPO}/pulls/{N}"), ("GET", f"{REPO}/pulls/{N}/reviews"),
                     ("GET", f"{REPO}/pulls/{N}"), ("GET", f"{REPO}/pulls/{N}/reviews"),
                     ("GET", f"{REPO}/pulls/{N}/reviews"), ("GET", f"{REPO}/pulls/{N}/reviews/11/comments")]
    assert daemon.switches.get("reviews") == "paused"
    leaked = [path for path in daemon.data.rglob("*") if path.is_file() and TOKEN.encode() in path.read_bytes()]
    assert leaked == []  # the token is in no file Cleave wrote


@respx.mock  # no route: any GitHub request fails
async def test_no_post_without_an_allowed_chat(tmp_path, capsys):
    """No default or project chat: the allowlist is empty and the bot is locked down. The reply in the reviewer chat
    is turned away: nothing runs, no answer goes out, and the ticket pipeline's parser never sees it."""
    daemon = Daemon(tmp_path, default_chat="")
    assert capsys.readouterr().out == IGNORED
    try:
        await daemon.reply("пости 1 2 5 апрув")
    finally:
        await daemon.github._client.aclose()

    assert daemon.parsed == []  # not the ticket intent parser's either: handle_message rejects the chat
    assert daemon.runs() == []  # the tool never ran
    assert daemon.notifier.sent == []  # no answer from the reviewer either
    assert daemon.store.job(daemon.job.id).state == "done" and daemon.store.posts(SCOPE, N) == []
    assert list(respx.calls) == []
