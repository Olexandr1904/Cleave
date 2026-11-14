# tests/integration/test_reviewer_job.py
"""The smoke run: one poll queues a review, one job runs it end to end — the real review clone over a local
bare origin, the real readiness commands (fake scripts), the real GitHub reader (respx), the real Claude Code
adapter starting a fake `claude` script, and the review message out to a fake Telegram notifier."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys

import respx

from config.schemas import (
    ProcessPartConfig,
    ReadinessPartConfig,
    ReviewerConfig,
    ReviewerGlobalConfig,
)
from integrations.github.github_adapter import GitHubAdapter
from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
from orchestrator.reviewer.chat import ReviewerChat
from orchestrator.reviewer.clone import ReviewClone
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.forge_github import GitHubForgeReader
from orchestrator.reviewer.prepare import job_paths
from orchestrator.reviewer.readiness import CommandReadiness
from orchestrator.reviewer.runner import JobRunner, RunnerConfig
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.session import SESSION_TOOLS, allowed_tools
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches

TOKEN = "ghp_smoke_not_real"
DOC = "docs/review/process.md"
PROCESS = (
    "# Review process\n\n## Prepare\n\nCleave prepares the PR.\n\n## Review\n\nReview the diff.\n\n"
    "## Re-check\n\nCheck the replies.\n"
)
API = "https://api.github.com/repos/acme-org/acme-app"
URL = "https://github.com/acme-org/acme-app/pull/7"
TITLE = "Fix cart sync ACME-1234"
UPDATED = "2026-11-20T09:00:00Z"
TG_LINE = "The date is parsed in the local zone, so it shifts by the UTC offset."
# The token as git_env sends it: a basic AUTHORIZATION header.
TOKEN_HEADER = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
# A fake `claude -p`: dumps its argv, environment and prompt, writes the report and tg_lines.json into the
# run directory its prompt names, and prints a stream-json result event.
FAKE_CLAUDE = r"""#!@PYTHON@
import json, os, re, sys
from pathlib import Path

prompt = sys.stdin.read()
Path(@DUMP@).write_text(json.dumps({"argv": sys.argv, "env": dict(os.environ), "prompt": prompt}))
run_dir = Path(re.search(r"Its artifacts are in (\S+): meta\.env", prompt)[1])
(run_dir / @REPORT_NAME@).write_text(@REPORT@, encoding="utf-8")
(run_dir / "tg_lines.json").write_text(json.dumps({"1": @TG_LINE@}), encoding="utf-8")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Report written.",
                  "num_turns": 12, "total_cost_usd": 1.5}))
"""


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(work, message):
    git("add", "-A", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", message, cwd=work)


def origin(tmp_path):
    """develop with the process document, and PR 7 (feature.txt on top of it) at refs/pull/7/head."""
    bare, work, pr_work = tmp_path / "origin.git", tmp_path / "work", tmp_path / "pr-work"
    git("init", "--bare", "-b", "develop", str(bare))
    git("clone", str(bare), str(work))
    (work / "docs" / "review").mkdir(parents=True)
    (work / DOC).write_text(PROCESS)
    commit(work, "process")
    git("push", "origin", "HEAD:develop", cwd=work)
    git("clone", str(bare), str(pr_work))
    (pr_work / "feature.txt").write_text("feature\n")
    commit(pr_work, "pr")
    git("push", "origin", "HEAD:refs/pull/7/head", cwd=pr_work)
    return bare, git("rev-parse", "HEAD", cwd=pr_work)


def script(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


def readiness_scripts(tmp_path, head):
    """Scripts printing the board snapshot (octo has a review turn on #7) and #7's threads snapshot."""
    folder = tmp_path / "readiness"
    folder.mkdir()
    board = {"me": "octo", "prs": [{"number": 7, "title": TITLE, "url": URL, "author": "octo-author",
                                    "draft": False, "turns": [{"login": "octo", "action": "review", "threads": []}]}]}
    threads = {"number": 7, "author": "octo-author", "head_sha": head, "threads": [], "review_bodies": []}
    for name, data in (("board", board), ("threads", threads)):
        (folder / f"{name}.json").write_text(json.dumps(data))
        script(folder / f"{name}.sh", f"#!/bin/sh\ncat '{folder / name}.json'\n")
    return ReadinessPartConfig(status_command=["sh", str(folder / "board.sh")],
                               threads_command=["sh", str(folder / "threads.sh"), "{pr}"])


def report_md(head):
    return (
        "# Review: PR #7 Fix cart sync\n\n"
        f"**Head SHA:** `{head}`\n"
        "**Candidates:** 1 -> 1 confirmed\n"
        "**Suggested event:** COMMENT - one minor finding\n\n"
        "## 🟡 Minor\n\n"
        "### [ ] 1. Date parsed in the local zone\n"
        "**File:** `feature.txt:1`\n\n"
        "## Summary\n"
        "One minor finding.\n"
    )


def mock_github(head):
    pr = {"number": 7, "title": TITLE, "body": "Keeps the cart in sync.", "html_url": URL,
          "user": {"login": "octo-author"}, "state": "open", "head": {"sha": head}, "base": {"ref": "develop"},
          "draft": False, "assignees": [], "requested_reviewers": [{"login": "octo"}], "changed_files": 1,
          "additions": 1, "deletions": 0, "updated_at": UPDATED}
    respx.get(f"{API}/pulls").respond(200, json=[{"number": 7, "updated_at": UPDATED}])
    respx.get(f"{API}/pulls/7").respond(200, json=pr)
    for path in ("pulls/7/reviews", "pulls/7/comments", "issues/7/comments"):
        respx.get(f"{API}/{path}").respond(200, json=[])


class Notifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return 100 + len(self.sent)


@respx.mock
async def test_review_job_end_to_end(tmp_path, monkeypatch):
    bare, head = origin(tmp_path)
    report_name = f"report_{head[:9]}.md"
    dump = tmp_path / "claude-call.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = script(bin_dir / "claude", FAKE_CLAUDE
                  .replace("@PYTHON@", sys.executable).replace("@DUMP@", repr(str(dump)))
                  .replace("@REPORT_NAME@", repr(report_name)).replace("@REPORT@", repr(report_md(head)))
                  .replace("@TG_LINE@", repr(TG_LINE)))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-real")
    llm = ClaudeCodeAdapter()
    # Never the real claude: the adapter must have resolved the fake before anything runs.
    assert shutil.which("claude") == str(fake)
    assert llm._claude_bin == str(fake)
    mock_github(head)

    rv = tmp_path / "rv"   # the reviewer data dir
    cfg = ReviewerConfig(enabled=True, base_branch="develop", quiet_minutes=0,
                         readiness=readiness_scripts(tmp_path, head),
                         process=ProcessPartConfig(doc=DOC, run_dir="tools/review/runs/PR_{pr}",
                                                   replies_plan="tools/threads/runs/PR_{pr}/replies_{head9}.json",
                                                   worktree_dir="../review-wt/PR_{pr}"))
    clone = ReviewClone(rv / "repos" / "acme" / "app", f"file://{bare}", "develop", TOKEN)
    ctx = RepoContext("acme", "app", cfg, clone,
                      CommandReadiness(cfg.readiness, repo="acme-org/acme-app", token=TOKEN),
                      GitHubForgeReader(GitHubAdapter(TOKEN, "acme-org", "acme-app")))
    conn = connect(rv / "reviewer.db")
    store, switches = ReviewerStore(conn), Switches(conn)
    notifier = Notifier()
    chat = ReviewerChat(notifier, "-1001")
    service = ReviewerService([ctx], store, switches, lambda key: False, chat.send,
                              lambda project_id, repo_id: set())
    runner = JobRunner([ctx], store, switches, lambda key: False, lambda key, status: None, chat, llm, {},
                       RunnerConfig(rv / "jobs", [tmp_path / ".env", tmp_path / "config"],
                                    ReviewerGlobalConfig().jobs))

    await service.poll(ctx)
    [queued] = store.jobs("acme/app")
    assert (queued.kind, queued.state, queued.head_sha) == ("review", "queued", head)
    job = runner.next_job()
    assert job.id == queued.id
    await runner.run_job(job)

    # The secret checks first, each computed outside its assert: a failing one shows names, indexes and
    # paths, never a value, and no later assert can print a value that holds the token.
    call = json.loads(dump.read_text())
    argv, env = call["argv"], call["env"]
    secrets = (TOKEN, TOKEN_HEADER)
    secret_names = sorted({"GH_TOKEN", "ANTHROPIC_API_KEY"} & env.keys())
    env_with_secret = sorted(name for name, value in env.items() if any(s in value for s in secrets))
    argv_with_secret = [i for i, arg in enumerate(argv) if any(s in arg for s in secrets)]
    prompt_has_secret = any(s in call["prompt"] for s in secrets)
    sent_with_secret = [i for i, (_, text) in enumerate(notifier.sent) if any(s in text for s in secrets)]
    files = [*(p for p in rv.rglob("*") if p.is_file() and not p.is_symlink()), dump]
    files_with_secret = [p for p in files if any(s.encode() in p.read_bytes() for s in secrets)]
    assert secret_names == []
    assert env_with_secret == []
    assert argv_with_secret == []
    assert prompt_has_secret is False
    assert sent_with_secret == []
    assert files_with_secret == []

    assert store.job(job.id).state == "done"
    [(chat_id, message)] = notifier.sent
    assert chat_id == "-1001"
    assert message.startswith(f'[acme/app] Review of PR #7 "{TITLE}"')
    assert TG_LINE in message

    paths = job_paths(clone.path, cfg.process, 7, head, rv / "jobs", job.id)
    rules = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Bash(git diff:*)" in rules
    assert f"Edit(/{paths.run_dir}/**)" in rules
    assert rules == allowed_tools("review", clone.path, paths, cfg.process.helper_scripts)
    assert argv == [
        str(fake), "-p",
        "--restricted", "--settings", str(paths.job_dir / "session" / "settings.json"), "--tools", SESSION_TOOLS,
        "--effort", "xhigh", "--model", "opus", "--output-format", "stream-json", "--verbose",
        "--max-turns", "400", "--allowedTools", ",".join(rules), "--add-dir", str(paths.worktree),
    ]
    assert not paths.worktree.exists()
    assert (paths.job_dir / "meta.env").read_text().startswith(f"HEAD={head}\n")
    assert (paths.job_dir / report_name).read_text(encoding="utf-8") == report_md(head)


# --- Code-review fix wave 1 ---

from datetime import datetime, timedelta, timezone

from orchestrator.reviewer.lines import login_line

# A fake `claude -p` that prints the given stream-json events and exits with the given code; stderr stays empty.
FAKE_CLAUDE_EVENTS = r"""#!@PYTHON@
import json, sys

sys.stdin.read()
for event in @EVENTS@:
    print(json.dumps(event))
sys.exit(@RC@)
"""


async def run_one_job(tmp_path, monkeypatch, events, rc):
    """The smoke run's poll and job, with the fake `claude` printing events: the store, the job, the notifier
    and the job runner's clock."""
    bare, head = origin(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = script(bin_dir / "claude", FAKE_CLAUDE_EVENTS.replace("@PYTHON@", sys.executable)
                  .replace("@EVENTS@", repr(events)).replace("@RC@", str(rc)))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    llm = ClaudeCodeAdapter()
    assert llm._claude_bin == str(fake)   # never the real claude
    mock_github(head)
    rv = tmp_path / "rv"
    cfg = ReviewerConfig(enabled=True, base_branch="develop", quiet_minutes=0,
                         readiness=readiness_scripts(tmp_path, head),
                         process=ProcessPartConfig(doc=DOC, run_dir="tools/review/runs/PR_{pr}",
                                                   replies_plan="tools/threads/runs/PR_{pr}/replies_{head9}.json",
                                                   worktree_dir="../review-wt/PR_{pr}"))
    clone = ReviewClone(rv / "repos" / "acme" / "app", f"file://{bare}", "develop", TOKEN)
    ctx = RepoContext("acme", "app", cfg, clone,
                      CommandReadiness(cfg.readiness, repo="acme-org/acme-app", token=TOKEN),
                      GitHubForgeReader(GitHubAdapter(TOKEN, "acme-org", "acme-app")))
    conn = connect(rv / "reviewer.db")
    store, switches = ReviewerStore(conn), Switches(conn)
    notifier = Notifier()
    chat = ReviewerChat(notifier, "-1001")
    service = ReviewerService([ctx], store, switches, lambda key: False, chat.send,
                              lambda project_id, repo_id: set())
    now = datetime.now(timezone.utc)
    runner = JobRunner([ctx], store, switches, lambda key: False, lambda key, status: None, chat, llm, {},
                       RunnerConfig(rv / "jobs", [tmp_path / ".env", tmp_path / "config"],
                                    ReviewerGlobalConfig().jobs), now=lambda: now)
    await service.poll(ctx)
    job = runner.next_job()
    await runner.run_job(job)
    return store, job, notifier, now


@respx.mock
async def test_login_failure_only_in_the_cli_result_event_defers_the_job(tmp_path, monkeypatch):
    """An expired claude.ai login: the CLI exits 1, stderr empty, the error only in its result event."""
    events = [{"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "total_cost_usd": 0,
               "result": "Failed to authenticate. API Error: 401 OAuth token has expired."}]
    store, job, notifier, now = await run_one_job(tmp_path, monkeypatch, events, rc=1)
    stored = store.job(job.id)
    assert (stored.state, stored.reason, stored.retry_at) == (
        "deferred", "claude login failing", (now + timedelta(minutes=15)).isoformat())
    assert [text for _, text in notifier.sent] == [login_line()]


@respx.mock
async def test_login_words_in_the_models_own_text_never_defer_the_job(tmp_path, monkeypatch):
    """The CLI exits 0 with an error result that has no text of its own: the model's last words are not the
    CLI's, even when they start like its login error."""
    events = [{"type": "assistant", "message": {"content": [
                  {"type": "text", "text": "Failed to authenticate is what AuthRepo.kt shows on a 401."}]}},
              {"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 3,
               "total_cost_usd": 0.5}]
    store, job, notifier, _ = await run_one_job(tmp_path, monkeypatch, events, rc=0)
    assert store.job(job.id).state == "failed"
    assert login_line() not in [text for _, text in notifier.sent]
