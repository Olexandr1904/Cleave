# tests/integration/test_reviewer_poll.py
from __future__ import annotations
import json
import subprocess
from datetime import datetime, timezone
from config.schemas import ProcessPartConfig, ReadinessPartConfig, ReviewerConfig
from orchestrator.reviewer.clone import ReviewClone
from orchestrator.reviewer.db import connect
from orchestrator.reviewer.models import PRDetails
from orchestrator.reviewer.readiness import CommandReadiness
from orchestrator.reviewer.service import RepoContext, ReviewerService
from orchestrator.reviewer.store import ReviewerStore
from orchestrator.switches import Switches

DOC = "docs/review/code-review-process.md"
SNAP = {"me": "octo", "prs": [{"number": 1, "title": "Fix 1 ACME-1", "url": "u1",
        "author": "octo-author", "draft": False,
        "turns": [{"login": "octo", "action": "review", "threads": []}]}]}

def git(*args, cwd=None):
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()

class FakeForge:
    async def open_prs(self): return {1: "t"}
    async def details(self, n, u):
        return PRDetails(1, "Fix 1 ACME-1", "u1", "octo-author", "open", "a" * 40, False, (),
                         ("octo",), 14, "t")
    async def reviewed_by(self, n, login, u): return False

async def test_poll_end_to_end(tmp_path):
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    git("init", "--bare", "-b", "develop", str(bare))
    git("clone", str(bare), str(work))
    (work / "docs" / "review").mkdir(parents=True)
    (work / DOC).write_text("# Process\n")
    (work / "tools").mkdir()
    status = work / "tools" / "status.sh"
    status.write_text(f"#!/bin/sh\necho '{json.dumps(SNAP)}'\n")
    status.chmod(0o755)
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init", cwd=work)
    git("push", "origin", "HEAD:develop", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work)

    cfg = ReviewerConfig(enabled=True, base_branch="develop",
                         readiness=ReadinessPartConfig(status_command=["sh", "tools/status.sh"],
                                                       threads_command=["sh", "tools/threads.sh"]),
                         process=ProcessPartConfig(doc=DOC))
    conn = connect(tmp_path / "rv" / "reviewer.db")
    store = ReviewerStore(conn)
    clone = ReviewClone(tmp_path / "rv" / "repos" / "app", f"file://{bare}", "develop", "tok")
    readiness = CommandReadiness(cfg.readiness, repo="acme-org/acme-app", token="tok")
    ctx = RepoContext("acme", "app", cfg, clone, readiness, FakeForge())
    service = ReviewerService([ctx], store, Switches(conn), lambda key: False, None,
                              lambda project_id, repo_id: set(),
                              now=lambda: datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc))
    await service.poll(ctx)
    state = store.repo_state("acme/app")
    assert (state.status, state.reason) == ("ok", "")
    assert list(store.board("acme/app")) == [1]
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone.path) == "HEAD"   # detached
    assert git("rev-parse", "HEAD", cwd=clone.path) == head
