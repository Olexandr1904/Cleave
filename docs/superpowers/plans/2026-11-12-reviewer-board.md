# Reviewer Mode — The Board (Plan 3 of 6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For every repo with reviews enabled, keep a live board of the PRs that involve the operator, with whose turn it is, send one Telegram line per new author-side turn, queue review and re-check jobs by the spec's rules, and show it all on a new Reviews tab — without running any model yet.

**Architecture:** A `ReviewerService` asyncio task next to `orchestrator.run()` polls each repo on its own interval: it refreshes a Cleave-owned review clone at the base branch (token only in that git subprocess's environment), runs the repo's readiness command there for the board snapshot, enriches it through a GitHub forge reader (open PRs, details cached by `updated_at`, the operator's reviews), and hands both to two pure functions — `build_board` (rows, roles, new and gone turns) and `plan_jobs` (queue / cancel / supersede, large-PR notices). State lives in `reviewer.db`. The job runner (Prepare, model run, report) is plan 4; jobs here stay `queued`.

**Tech Stack:** Python 3.12, asyncio subprocesses, `sqlite3`, httpx + respx, git (local bare repos in tests), Starlette, vanilla JS modules.

**Spec:** `docs/superpowers/specs/2026-11-12-reviewer-mode-design.md` — "Shape", "Part 1 — Readiness", "Part 2" (review clone, step 1 of Prepare; Large PRs), "Core behaviour": "Switches", "The board", "When a review starts", "Telegram" (turn lines), "Dashboard — the Reviews tab", "Credential health" (Effect), "State", "Failure handling", "Decided with the operator" (Language). Plan 2 (`docs/superpowers/plans/2026-11-12-reviewer-config-switches-health.md`) is merged: `ReviewerConfig`, `Switches`, `CredentialHealth`, `reviewer.db` migrations, `resolve.operator_chat_id`.

**Later plans:** 4 — review job (runner semaphore, Prepare steps 1b–5 on top of `ReviewClone`, model run, parse, review message, dashboard detail, failure handling); 5 — posting from Telegram (reply anchors, grammar, «ревью заново» on any line, poster); 6 — deployment.

## Global Constraints

- No employer names anywhere: examples use `acme`, `acme-org/acme-app`, `ACME-1234`, login `octo`.
- Everything the operator sees is English (spec, "Decided with the operator — Language"); Russian/Ukrainian words only as input synonyms.
- A token value never appears in a log line, a `reason`, an API response, a Telegram line, a test assertion message or a file on disk. Text taken from a subprocess's stderr is passed through `redact(text, [token])` before it is stored or shown.
- Network git calls get the token only through the environment of that one subprocess: `GIT_CONFIG_NOSYSTEM=1`, `credential.helper` emptied, `http.https://github.com/.extraheader` set via `GIT_CONFIG_*` — no token in `.git/config`, no keychain (spec, Prepare step 1). The repo YAML's `git.clone_url` (it embeds the token) is never used here.
- Part commands run from the review clone at the base branch with `GH_TOKEN` and `GITHUB_TOKEN` set to the repo's forge token, in that subprocess only (spec, Part 1 "Kind command"; the reference tool calls `gh`).
- Subprocesses: `asyncio.create_subprocess_exec` only, own session, process group killed on timeout, always awaited.
- Tests never touch the network and never start `claude`: git tests use local bare repositories (`file://`), GitHub calls use respx, part commands are fake scripts in `tmp_path`.
- Reviewer state lives in `reviewer.db`; the review clone is `<reviewer data dir>/repos/<project_id>/<repo_id>` where the data dir is `reviewer_db_path(cfg).parent` (default `~/CleaveData/reviewer`); nothing is named `state.json`; nothing under `workspaces.base_dir`.
- Times are UTC ISO-8601 strings in the store; every time-dependent function takes `now` as a parameter.
- Reviews paused (switch): the board keeps refreshing, no job is queued automatically; "Review now" still queues (plan 4's runner will not start it while paused). Turn lines are still sent while paused.
- Job states are exactly `queued`, `running`, `done`, `posted`, `failed`, `deferred`, `superseded`, `skipped`, `cancelled`, `stale`; this plan writes only `queued`, `superseded`, `skipped`, `cancelled`, `stale`.
- `PY` is a Python 3.12 venv with `pip install -e '.[dev]'`; suite: `$PY -m pytest tests/unit tests/integration -q`; the 9 pre-existing failures (`test_not_a_git_dir`, 3× `test_main.py`, 3× `test_e2e_dry_run.py`, `test_build_jql_ands_multiple_trigger_labels`, `test_bad_payload_returns_400`) are not regressions. `tests/e2e` needs playwright and is not run.
- Branch `reviewer-board` off `master` (`git switch -c`). No commit and no push unless the operator asks; then group into logical commits.

## Review Focus

- **A token in a failing command's stderr** (gh or git echoing a URL) — expected: redacted before it reaches the repo status, a Telegram line or the API. Pinned by `test_readiness_failure_reason_is_redacted` (Task 2) and `test_clone_error_is_redacted` (Task 3).
- **A readiness command that hangs** — expected: killed after the timeout, repo status `readiness_failed`, one line, back-off, the next repos still polled. Pinned by `test_readiness_timeout` (Task 2) and `test_failing_repo_does_not_block_others` (Task 7).
- **The first poll after deployment** with many existing turns — expected: no Telegram flood (author-side turns recorded silently) while review and re-check jobs are still queued. Pinned by `test_first_poll_baseline` (Task 6).
- **A PR closed between the readiness snapshot and the forge list** — expected: dropped from the board, its queued jobs cancelled. Pinned by `test_closed_pr_dropped` (Task 5) and `test_gone_pr_cancels_queued` (Task 6).
- **More than 100 open PRs** — expected: every page read. Pinned by `test_open_prs_paginates` (Task 4).

---

### Task 1: Store — board, jobs, skips, notices, repo state

**Files:**
- Modify: `orchestrator/reviewer/db.py` (append migration 2 to `MIGRATIONS`)
- Create: `orchestrator/reviewer/models.py`, `orchestrator/reviewer/store.py`
- Test: `tests/unit/test_reviewer_store.py`

**Interfaces:**
- Produces (`models.py`, dataclasses):
  - `Turn(login: str, action: str, threads: tuple[str, ...] = (), reviewer: str = "")` (frozen)
  - `SnapshotPR(number: int, title: str, url: str, author: str, draft: bool, threads_truncated: bool, turns: tuple[Turn, ...], extra: dict)` (frozen; `extra` = snapshot fields the core does not use)
  - `BoardSnapshot(me: str, prs: tuple[SnapshotPR, ...])` (frozen)
  - `PRDetails(number: int, title: str, url: str, author: str, state: str, head_sha: str, draft: bool, assignees: tuple[str, ...], requested_reviewers: tuple[str, ...], changed_files: int, updated_at: str)` (frozen)
  - `BoardRow(number, title, url, author, roles: list[str], head_sha, head_first_seen_at, draft: bool, changed_files: int, my_turns: list[dict], other_turns: list[dict], threads_truncated: bool, pipeline: bool, ticket_key: str, updated_at: str, last_change: str)` with `to_json() -> str` and `from_json(text) -> BoardRow`; `my_turns` items `{"action": str, "threads": list[str]}`, `other_turns` items `{"login": str, "action": str}`; `my_actions` property → `list[str]`
  - `RepoState(scope: str, first_poll_done: bool = False, status: str = "new", reason: str = "", failures: int = 0, last_poll_at: str = "", last_ok_at: str = "", next_poll_at: str = "")`; statuses `new`, `ok`, `token_failing`, `readiness_failed`, `doc_missing`, `error`
  - `Job(id: str, scope: str, number: int, kind: str, head_sha: str, key: str, state: str, reason: str, created_at: str, updated_at: str)`; `JOB_STATES` tuple as in Global Constraints
- Produces (`store.py`): `ReviewerStore(conn: sqlite3.Connection)`:
  - `board(scope) -> dict[int, BoardRow]`; `replace_board(scope, rows: dict[int, BoardRow]) -> None` (one transaction; rows not in `rows` are deleted)
  - `repo_state(scope) -> RepoState` (default `RepoState(scope)`); `save_repo_state(state) -> None`
  - `add_job(scope, number, kind, head_sha, key, now: str) -> Job` (id = `uuid4().hex[:12]`, state `queued`, reason `""`)
  - `jobs(scope, number: int | None = None) -> list[Job]` (newest first); `set_job_state(job_id, state, reason: str, now: str) -> None` (`ValueError` for a state not in `JOB_STATES`)
  - `skip(scope, number, head_sha, now) -> None`; `is_skipped(scope, number, head_sha) -> bool`
  - `notice_sent(scope, number, kind, key) -> bool`; `mark_notice(scope, number, kind, key, now) -> None`; `clear_notice(scope, number, kind) -> None` (all keys of that kind)
  - writes hold a `threading.Lock` (same pattern as `Switches`)
- Migration 2 (one `MIGRATIONS` entry, several statements):
  `CREATE TABLE board_prs (scope TEXT NOT NULL, number INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY (scope, number)); CREATE TABLE repo_state (scope TEXT PRIMARY KEY, data TEXT NOT NULL); CREATE TABLE jobs (id TEXT PRIMARY KEY, scope TEXT NOT NULL, number INTEGER NOT NULL, kind TEXT NOT NULL, head_sha TEXT NOT NULL, key TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL); CREATE INDEX jobs_pr ON jobs (scope, number); CREATE TABLE skips (scope TEXT NOT NULL, number INTEGER NOT NULL, head_sha TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (scope, number, head_sha)); CREATE TABLE notices (scope TEXT NOT NULL, number INTEGER NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, sent_at TEXT NOT NULL, PRIMARY KEY (scope, number, kind, key))`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_reviewer_store.py
from __future__ import annotations
import pytest
from orchestrator.reviewer.db import MIGRATIONS, connect
from orchestrator.reviewer.models import BoardRow, RepoState
from orchestrator.reviewer.store import ReviewerStore

T0, T1 = "2026-11-20T10:00:00+00:00", "2026-11-20T10:05:00+00:00"

def row(n=1712, head="a" * 40, actions=("review",)):
    return BoardRow(n, "Fix cart sync ACME-1234", f"https://github.com/acme-org/acme-app/pull/{n}", "octo-author",
                    ["reviewer"], head, T0, False, 14, [{"action": a, "threads": []} for a in actions], [], False,
                    False, "ACME-1234", T0, T0)

@pytest.fixture
def store(tmp_path):
    return ReviewerStore(connect(tmp_path / "rv" / "reviewer.db"))

def test_migration_2_applied(tmp_path):
    conn = connect(tmp_path / "r.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 2

def test_board_roundtrip_and_replace(store):
    store.replace_board("acme/app", {1712: row(), 1720: row(1720)})
    assert store.board("acme/app")[1712] == row()
    assert store.board("acme/app")[1712].my_actions == ["review"]
    store.replace_board("acme/app", {1720: row(1720)})
    assert list(store.board("acme/app")) == [1720]
    assert store.board("other/repo") == {}

def test_repo_state_default_and_save(store):
    assert store.repo_state("acme/app") == RepoState("acme/app")
    store.save_repo_state(RepoState("acme/app", True, "ok", "", 0, T1, T1, T1))
    assert store.repo_state("acme/app").first_poll_done

def test_jobs(store):
    j = store.add_job("acme/app", 1712, "review", "a" * 40, "a" * 40, T0)
    assert (j.state, j.reason, len(j.id)) == ("queued", "", 12)
    store.add_job("acme/app", 1712, "recheck", "a" * 40, "k", T1)
    assert [x.kind for x in store.jobs("acme/app", 1712)] == ["recheck", "review"]
    store.set_job_state(j.id, "cancelled", "turn gone", T1)
    assert store.jobs("acme/app", 1712)[1].state == "cancelled"
    with pytest.raises(ValueError):
        store.set_job_state(j.id, "exploded", "", T1)

def test_skips_and_notices(store):
    store.skip("acme/app", 1712, "a" * 40, T0)
    assert store.is_skipped("acme/app", 1712, "a" * 40) and not store.is_skipped("acme/app", 1712, "b" * 40)
    assert not store.notice_sent("acme/app", 1712, "large", "a" * 40)
    store.mark_notice("acme/app", 1712, "large", "a" * 40, T0)
    assert store.notice_sent("acme/app", 1712, "large", "a" * 40)
    store.clear_notice("acme/app", 1712, "large")
    assert not store.notice_sent("acme/app", 1712, "large", "a" * 40)
```

- [ ] **Step 2: Run to verify they fail** — `$PY -m pytest tests/unit/test_reviewer_store.py -q` → FAIL, `ModuleNotFoundError: orchestrator.reviewer.models`.
- [ ] **Step 3: Implement** the migration, `models.py` and `store.py` as in the Interfaces block. `BoardRow` JSON uses `dataclasses.asdict`.
- [ ] **Step 4: Run** — `$PY -m pytest tests/unit/test_reviewer_store.py tests/unit/test_switches.py -q` → all pass (the switches tests must still see a migrated DB; adjust only an assertion that hard-codes `user_version == 1`, and ledger it).

---

### Task 2: Part commands and the `command` readiness part

**Files:**
- Create: `orchestrator/reviewer/commands.py`, `orchestrator/reviewer/readiness.py`
- Test: `tests/unit/test_reviewer_readiness.py`

**Interfaces:**
- Consumes: `ReadinessPartConfig` (plan 2), `REVIEWER_PLACEHOLDERS`; `BoardSnapshot`, `SnapshotPR`, `Turn` (Task 1)
- Produces (`commands.py`, reused by plans 4–5):
  - `base_env() -> dict[str, str]` — `HOME`, `PATH`, `LANG`, `TMPDIR`, `USER` copied from `os.environ` when set
  - `part_env(token: str) -> dict[str, str]` — `base_env()` + `GH_TOKEN`, `GITHUB_TOKEN` = token, `GH_PROMPT_DISABLED=1`, `GIT_TERMINAL_PROMPT=0`, `NO_COLOR=1`
  - `expand(template: list[str], values: dict[str, str]) -> list[str]` — `str.format_map` per element; a placeholder missing from `values` raises `ValueError` naming it
  - `redact(text: str, secrets: list[str]) -> str` — every non-empty secret replaced by `***`
  - `@dataclass CommandResult(returncode: int, stdout: str, stderr: str, timed_out: bool)`
  - `async run_command(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> CommandResult` — own session; on timeout kill the process group, await it, `timed_out=True`, `returncode=-9`; a missing executable gives `returncode=127`, `stderr=f"not found: {argv[0]}"`
  - `stderr_tail(text: str, limit: int = 200) -> str` — last non-empty line, cut to `limit`
- Produces (`readiness.py`):
  - `class ReadinessError(Exception)`
  - `parse_board_snapshot(text: str) -> BoardSnapshot` — required: `me` str, `prs` list; per PR `number` int, `title` str, `url` str, `author` str, `draft` bool, `turns` list; `threads_truncated` defaults to `False`; per turn `login` str, `action` str, `threads` list of str (default `()`), `reviewer` str (default `""`); other PR keys go to `extra`; any violation → `ReadinessError("board snapshot: <where>: <what>")`, e.g. `"board snapshot: prs[0].number: expected int"`
  - `parse_threads_snapshot(text: str) -> dict` — requires `number` int, `author` str, `head_sha` 40 hex, `threads` list, `review_bodies` list; returns the parsed dict (plan 4 consumes it)
  - `class CommandReadiness(cfg: ReadinessPartConfig, *, repo: str, token: str, timeout: float = 300.0)` with `async board(clone_dir: Path) -> BoardSnapshot` (runs `expand(cfg.status_command, {"repo": repo})`) and `async threads(clone_dir: Path, pr: int) -> dict` (`{"repo": repo, "pr": str(pr)}`); cwd = `clone_dir`, env = `part_env(token)`; non-zero exit → `ReadinessError(f"exit {rc}: {redact(stderr_tail(stderr), [token])}")`; timeout → `ReadinessError(f"timed out after {int(timeout)} s")`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_reviewer_readiness.py
from __future__ import annotations
import json, stat, time
import pytest
from config.schemas import ReadinessPartConfig
from orchestrator.reviewer.commands import expand, part_env, redact, run_command
from orchestrator.reviewer.readiness import (CommandReadiness, ReadinessError, parse_board_snapshot,
                                             parse_threads_snapshot)

SNAP = {"me": "octo", "prs": [{"number": 1712, "title": "Fix cart sync", "url": "u", "author": "octo-author",
        "draft": False, "threads_truncated": False, "checks": "SUCCESS",
        "turns": [{"login": "octo", "action": "review", "threads": []},
                  {"login": "octo-author", "action": "address_review", "threads": [], "reviewer": "octo"}]}]}

def script(tmp_path, body):
    p = tmp_path / "tool.sh"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)

def test_parse_board_snapshot():
    s = parse_board_snapshot(json.dumps(SNAP))
    assert s.me == "octo" and s.prs[0].number == 1712 and s.prs[0].extra == {"checks": "SUCCESS"}
    assert [(t.login, t.action, t.reviewer) for t in s.prs[0].turns] == [("octo", "review", ""),
                                                                         ("octo-author", "address_review", "octo")]

@pytest.mark.parametrize("bad,where", [
    ("not json", "board snapshot"), ({"prs": []}, "me"), ({"me": "o", "prs": [{"number": "1"}]}, "prs[0].number"),
    ({"me": "o", "prs": [{**SNAP["prs"][0], "turns": [{"login": "o"}]}]}, "prs[0].turns[0].action"),
])
def test_parse_board_snapshot_rejects(bad, where):
    with pytest.raises(ReadinessError, match=where.replace("[", r"\[").replace("]", r"\]")):
        parse_board_snapshot(bad if isinstance(bad, str) else json.dumps(bad))

def test_parse_threads_snapshot():
    t = parse_threads_snapshot(json.dumps({"number": 1, "author": "a", "head_sha": "c" * 40, "threads": [],
                                           "review_bodies": []}))
    assert t["head_sha"] == "c" * 40
    with pytest.raises(ReadinessError):
        parse_threads_snapshot(json.dumps({"number": 1, "author": "a", "head_sha": "xyz", "threads": [],
                                           "review_bodies": []}))

def test_expand_and_env_and_redact(monkeypatch):
    assert expand(["t.py", "--repo", "{repo}", "{pr}"], {"repo": "o/r", "pr": "5"}) == ["t.py", "--repo", "o/r", "5"]
    with pytest.raises(ValueError, match="pr"):
        expand(["{pr}"], {"repo": "o/r"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    env = part_env("tok")
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "tok" and "ANTHROPIC_API_KEY" not in env
    assert redact("auth tok failed for tok", ["tok", ""]) == "auth *** failed for ***"

async def test_board_runs_status_command(tmp_path):
    tool = script(tmp_path, f'[ "$1 $2" = "--repo acme-org/acme-app" ] || exit 9\n'
                            f'[ -n "$GH_TOKEN" ] || exit 8\necho \'{json.dumps(SNAP)}\'\n')
    r = CommandReadiness(ReadinessPartConfig(status_command=[tool, "--repo", "{repo}"], threads_command=[tool]),
                         repo="acme-org/acme-app", token="tok")
    assert (await r.board(tmp_path)).me == "octo"

async def test_readiness_failure_reason_is_redacted(tmp_path):
    tool = script(tmp_path, 'echo "fatal: https://x:SECRET@github.com denied" >&2\nexit 3\n')
    r = CommandReadiness(ReadinessPartConfig(status_command=[tool], threads_command=[tool]), repo="o/r", token="SECRET")
    with pytest.raises(ReadinessError) as e:
        await r.board(tmp_path)
    assert str(e.value).startswith("exit 3: ") and "SECRET" not in str(e.value)

async def test_readiness_timeout(tmp_path):
    tool = script(tmp_path, "sleep 30\n")
    r = CommandReadiness(ReadinessPartConfig(status_command=[tool], threads_command=[tool]), repo="o/r", token="t",
                         timeout=0.5)
    t0 = time.monotonic()
    with pytest.raises(ReadinessError, match="timed out after 0 s"):
        await r.board(tmp_path)
    assert time.monotonic() - t0 < 5

async def test_run_command_missing_executable(tmp_path):
    res = await run_command([str(tmp_path / "nope")], cwd=tmp_path, env={}, timeout=5)
    assert (res.returncode, res.stderr) == (127, f"not found: {tmp_path / 'nope'}")
```

- [ ] **Step 2: Run to verify they fail** — `$PY -m pytest tests/unit/test_reviewer_readiness.py -q` → FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `commands.py` and `readiness.py`.
- [ ] **Step 4: Run** — same command → all pass.

---

### Task 3: The review clone

**Files:**
- Create: `orchestrator/reviewer/clone.py`
- Test: `tests/unit/test_reviewer_clone.py`

**Interfaces:**
- Consumes: `base_env`, `run_command`, `redact`, `stderr_tail` (Task 2)
- Produces:
  - `git_env(token: str) -> dict[str, str]` — `base_env()` + `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_COUNT=2`, `GIT_CONFIG_KEY_0=credential.helper`, `GIT_CONFIG_VALUE_0=""`, `GIT_CONFIG_KEY_1=http.https://github.com/.extraheader`, `GIT_CONFIG_VALUE_1="AUTHORIZATION: basic " + base64("x-access-token:" + token)`
  - `class CloneError(Exception)`
  - `class ReviewClone(path: Path, url: str, base_branch: str, token: str, timeout: float = 300.0)`:
    - `async refresh() -> str` — if `path/.git` is missing: `git clone --branch <base> --single-branch <url> <path>`; else `git -C <path> fetch --prune origin +refs/heads/<base>:refs/remotes/origin/<base>`; then `git -C <path> -c advice.detachedHead=false switch --detach --discard-changes origin/<base>`; returns `git rev-parse HEAD`. Every git call runs with `git_env(token)`; a failure raises `CloneError(f"git <verb> failed: {redact(stderr_tail(stderr), [token])}")`.
    - `has_file(rel: str) -> bool` — `(path / rel).is_file()` after `refresh`
  - `github_clone_url(owner: str, repo: str) -> str` → `https://github.com/{owner}/{repo}.git`

- [ ] **Step 1: Write the failing tests** (git runs locally on `file://` bare repos; the fixture builds one with `subprocess.run` — test code may block)

```python
# tests/unit/test_reviewer_clone.py
from __future__ import annotations
import base64, subprocess
import pytest
from orchestrator.reviewer.clone import CloneError, ReviewClone, git_env, github_clone_url

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

@pytest.fixture
def origin(tmp_path):
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    git("init", "--bare", "-b", "develop", str(bare))
    git("clone", str(bare), str(work))
    (work / "docs").mkdir()
    (work / "docs" / "process.md").write_text("# Process\n")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "a", cwd=work)
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "b", cwd=work)
    git("push", "origin", "HEAD:develop", cwd=work)
    return bare, work

def test_git_env_carries_token_only_as_header(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "leak")
    env = git_env("tok")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_VALUE_1"] == "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:tok").decode()
    assert "GH_TOKEN" not in env

def test_git_env_empties_global_helper(tmp_path, monkeypatch):
    (tmp_path / ".gitconfig").write_text("[credential]\n\thelper = osxkeychain\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    out = subprocess.run(["git", "config", "--get-all", "credential.helper"], env=git_env("tok"),
                         capture_output=True, text=True).stdout.splitlines()
    assert out[-1] == ""          # the last value wins: helpers reset

async def test_refresh_clones_then_fetches(tmp_path, origin):
    bare, work = origin
    clone = ReviewClone(tmp_path / "clone", f"file://{bare}", "develop", "tok")
    sha = await clone.refresh()
    assert sha == git("rev-parse", "HEAD", cwd=work) and clone.has_file("docs/process.md")
    assert "tok" not in (tmp_path / "clone" / ".git" / "config").read_text()
    (work / "new.txt").write_text("x")
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "c", cwd=work)
    git("push", "origin", "HEAD:develop", cwd=work)
    assert await clone.refresh() == git("rev-parse", "HEAD", cwd=work)
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=tmp_path / "clone") == "HEAD"   # detached

async def test_clone_error_is_redacted(tmp_path):
    clone = ReviewClone(tmp_path / "clone", f"file://{tmp_path}/missing-SECRET.git", "develop", "SECRET")
    with pytest.raises(CloneError) as e:
        await clone.refresh()
    assert str(e.value).startswith("git clone failed: ") and "SECRET" not in str(e.value)

def test_github_clone_url():
    assert github_clone_url("acme-org", "acme-app") == "https://github.com/acme-org/acme-app.git"
```

- [ ] **Step 2: Run to verify they fail** — `$PY -m pytest tests/unit/test_reviewer_clone.py -q` → FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `clone.py`.
- [ ] **Step 4: Run** — same command → all pass.

---

### Task 4: GitHub forge reader

**Files:**
- Create: `orchestrator/reviewer/forge_github.py`
- Test: `tests/unit/test_reviewer_forge_github.py`

**Interfaces:**
- Consumes: `GitHubAdapter` (`_request(method, path, **kwargs)`, `_repo_path`), `PRDetails` (Task 1)
- Produces:
  - `class ForgeError(Exception)` — message `GitHub <status>` for HTTP errors, the exception class name otherwise (never a response body)
  - `class GitHubForgeReader(adapter: GitHubAdapter)`:
    - `async open_prs() -> dict[int, str]` — number → `updated_at`; `GET {repo_path}/pulls` with `params={"state": "open", "per_page": 100, "page": k}`, k = 1, 2, … until a page has fewer than 100 items
    - `async details(number: int, updated_at: str) -> PRDetails` — `GET {repo_path}/pulls/{number}`; cached per `(number, updated_at)`; fields from `number`, `title`, `html_url`, `user.login`, `state`, `head.sha`, `draft`, `assignees[].login`, `requested_reviewers[].login`, `changed_files`, `updated_at`
    - `async reviewed_by(number: int, login: str, updated_at: str) -> bool` — `GET {repo_path}/pulls/{number}/reviews` paginated like `open_prs`; true if any review by `login` has a state other than `PENDING`; cached per `(number, login, updated_at)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_reviewer_forge_github.py
from __future__ import annotations
import httpx, pytest, respx
from integrations.github.github_adapter import GitHubAdapter
from orchestrator.reviewer.forge_github import ForgeError, GitHubForgeReader

BASE = "https://api.github.com/repos/acme-org/acme-app"
PR = {"number": 1712, "title": "Fix cart sync", "html_url": "https://github.com/acme-org/acme-app/pull/1712",
      "user": {"login": "octo-author"}, "state": "open", "head": {"sha": "a" * 40}, "draft": False,
      "assignees": [{"login": "octo"}], "requested_reviewers": [{"login": "octo2"}], "changed_files": 14,
      "updated_at": "2026-11-20T10:00:00Z"}

def reader():
    return GitHubForgeReader(GitHubAdapter("tok", "acme-org", "acme-app"))

@respx.mock
async def test_open_prs_paginates():
    route = respx.get(f"{BASE}/pulls")
    route.side_effect = [httpx.Response(200, json=[{"number": i, "updated_at": "t"} for i in range(100)]),
                         httpx.Response(200, json=[{"number": 100, "updated_at": "t2"}])]
    prs = await reader().open_prs()
    assert len(prs) == 101 and prs[100] == "t2"
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]

@respx.mock
async def test_details_cached_by_updated_at():
    route = respx.get(f"{BASE}/pulls/1712").respond(200, json=PR)
    r = reader()
    d = await r.details(1712, "2026-11-20T10:00:00Z")
    await r.details(1712, "2026-11-20T10:00:00Z")
    assert route.call_count == 1
    assert (d.author, d.head_sha, d.assignees, d.requested_reviewers, d.changed_files) == (
        "octo-author", "a" * 40, ("octo",), ("octo2",), 14)
    await r.details(1712, "2026-11-20T11:00:00Z")
    assert route.call_count == 2

@respx.mock
async def test_reviewed_by():
    respx.get(f"{BASE}/pulls/1712/reviews").respond(200, json=[{"user": {"login": "octo"}, "state": "PENDING"},
                                                              {"user": {"login": "x"}, "state": "APPROVED"}])
    assert await reader().reviewed_by(1712, "octo", "t") is False
    respx.get(f"{BASE}/pulls/1713/reviews").respond(200, json=[{"user": {"login": "octo"}, "state": "COMMENTED"}])
    assert await reader().reviewed_by(1713, "octo", "t") is True

@respx.mock
async def test_errors_become_forge_error_without_body():
    respx.get(f"{BASE}/pulls/9").respond(401, json={"message": "Bad credentials tok"})
    with pytest.raises(ForgeError, match="^GitHub 401$"):
        await reader().details(9, "t")
```

- [ ] **Step 2: Run to verify they fail** — `$PY -m pytest tests/unit/test_reviewer_forge_github.py -q` → FAIL, `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `forge_github.py`. A `RuntimeError` from `_request` (5xx after retries) carries the status in its text; extract the number with `re.search(r"→ (\d{3})", str(e))` for the message, else use the class name.
- [ ] **Step 4: Run** — same command → all pass.

---

### Task 5: The board (pure)

**Files:**
- Create: `orchestrator/reviewer/board.py`
- Test: `tests/unit/test_reviewer_board.py`

**Interfaces:**
- Consumes: `BoardSnapshot`, `PRDetails`, `BoardRow` (Task 1)
- Produces:
  - `JOB_ACTIONS = frozenset({"review", "recheck"})`; `TICKET_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")`
  - `@dataclass(frozen=True) TurnChange(number: int, action: str, threads: tuple[str, ...])`
  - `@dataclass BoardUpdate(rows: dict[int, BoardRow], new_turns: list[TurnChange], gone_turns: list[TurnChange])`
  - `build_board(snapshot: BoardSnapshot, details: dict[int, PRDetails], reviewed: dict[int, bool], previous: dict[int, BoardRow], pipeline_prs: set[int], now: str) -> BoardUpdate`
- Rules:
  - a snapshot PR without an entry in `details` (not open any more) is dropped;
  - `roles`, in this order, from `details` and `snapshot.me`: `author` (details author == me), `assignee` (me in assignees), `reviewer` (me in requested reviewers, or `reviewed[number]`); a PR with no role is dropped;
  - `my_turns` = the snapshot turns with `login == me`, in snapshot order, as `{"action", "threads": list}`; `other_turns` = the rest as `{"login", "action"}`;
  - `head_sha`, `draft`, `changed_files`, `updated_at`, `title`, `url`, `author` come from `details`;
  - `head_first_seen_at` = the previous row's value when its `head_sha` equals the new one, else `now`;
  - `last_change` = `now` when `roles`, `my_turns`, `other_turns`, `head_sha` or `draft` differ from the previous row (or there is none), else the previous value;
  - `pipeline` = `number in pipeline_prs`; `ticket_key` = the first `TICKET_KEY` match in the title, else `""`;
  - `new_turns`: for each row, my-turn actions not among the previous row's my-turn actions (a PR new to the board counts all of them); `gone_turns`: previous my-turn actions no longer present, including every action of a previous row that is no longer on the board.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_reviewer_board.py
from __future__ import annotations
from orchestrator.reviewer.board import build_board
from orchestrator.reviewer.models import BoardSnapshot, PRDetails, SnapshotPR, Turn

T0, T1 = "2026-11-20T10:00:00+00:00", "2026-11-20T10:05:00+00:00"

def snap(*prs, me="octo"):
    return BoardSnapshot(me, tuple(prs))

def spr(n, turns, author="octo-author"):
    return SnapshotPR(n, f"Fix {n} ACME-{n}", f"u{n}", author, False, False, tuple(turns), {})

def det(n, head="a" * 40, author="octo-author", assignees=(), requested=("octo",), files=14):
    return PRDetails(n, f"Fix {n} ACME-{n}", f"u{n}", author, "open", head, False, tuple(assignees),
                     tuple(requested), files, "t")

def test_roles_turns_and_fields():
    u = build_board(snap(spr(1, [Turn("octo", "review"), Turn("octo-author", "fix_checks")])),
                    {1: det(1)}, {1: False}, {}, set(), T0)
    r = u.rows[1]
    assert r.roles == ["reviewer"] and r.my_turns == [{"action": "review", "threads": []}]
    assert r.other_turns == [{"login": "octo-author", "action": "fix_checks"}]
    assert (r.ticket_key, r.head_first_seen_at, r.last_change, r.pipeline) == ("ACME-1", T0, T0, False)
    assert [(t.number, t.action) for t in u.new_turns] == [(1, "review")] and u.gone_turns == []

def test_membership_rules():
    s = snap(spr(1, []), spr(2, []), spr(3, []), spr(4, []), spr(5, [], author="octo"))
    d = {1: det(1, requested=()), 2: det(2, requested=(), assignees=("octo",)), 3: det(3, requested=()),
         4: det(4), 5: det(5, author="octo", requested=())}
    u = build_board(s, d, {1: False, 2: False, 3: True, 4: False, 5: False}, {}, set(), T0)
    assert sorted(u.rows) == [2, 3, 4, 5]
    assert (u.rows[2].roles, u.rows[3].roles, u.rows[5].roles) == (["assignee"], ["reviewer"], ["author"])

def test_closed_pr_dropped():
    u = build_board(snap(spr(1, [Turn("octo", "review")])), {}, {}, {}, set(), T0)
    assert u.rows == {} and u.new_turns == []

def test_head_first_seen_and_last_change():
    first = build_board(snap(spr(1, [Turn("octo", "review")])), {1: det(1)}, {1: False}, {}, set(), T0).rows
    same = build_board(snap(spr(1, [Turn("octo", "review")])), {1: det(1)}, {1: False}, first, set(), T1)
    assert (same.rows[1].head_first_seen_at, same.rows[1].last_change, same.new_turns) == (T0, T0, [])
    pushed = build_board(snap(spr(1, [Turn("octo", "review")])), {1: det(1, head="b" * 40)}, {1: False}, first, set(), T1)
    assert (pushed.rows[1].head_first_seen_at, pushed.rows[1].last_change) == (T1, T1)

def test_gone_turns_include_dropped_prs():
    prev = build_board(snap(spr(1, [Turn("octo", "review")]), spr(2, [Turn("octo", "merge")], author="octo")),
                       {1: det(1), 2: det(2, author="octo", requested=())}, {}, {}, set(), T0).rows
    u = build_board(snap(spr(1, [])), {1: det(1)}, {}, prev, set(), T1)
    assert sorted((t.number, t.action) for t in u.gone_turns) == [(1, "review"), (2, "merge")]

def test_pipeline_flag():
    u = build_board(snap(spr(7, [Turn("octo", "answer_threads", ("T1", "T2"))], author="octo")),
                    {7: det(7, author="octo", requested=())}, {}, {}, {7}, T0)
    assert u.rows[7].pipeline and u.new_turns[0].threads == ("T1", "T2")
```

- [ ] **Step 2: Run to verify they fail** — `$PY -m pytest tests/unit/test_reviewer_board.py -q` → FAIL.
- [ ] **Step 3: Implement** `board.py`.
- [ ] **Step 4: Run** — same command → all pass.

---

### Task 6: Job planning and turn lines (pure)

**Files:**
- Create: `orchestrator/reviewer/jobs.py`, `orchestrator/reviewer/lines.py`
- Test: `tests/unit/test_reviewer_jobs.py`

**Interfaces:**
- Consumes: `BoardRow`, `Job` (Task 1); `BoardUpdate`, `TurnChange`, `JOB_ACTIONS` (Task 5); `ReviewerConfig` (plan 2)
- Produces (`lines.py`, exact copy):
  - `turn_line(scope: str, row: BoardRow, change: TurnChange) -> str` — `answer_threads`: `[{scope}] #{n}: replies in {k} thread(s) — handle them locally · {url}` (k = `len(change.threads)`); any other action: `[{scope}] #{n}: your turn — {action} · {url}`
  - `large_line(scope, row) -> str` — `[{scope}] #{n} is large ({changed_files} files) — no automatic review · {url}`
  - `readiness_failed_line(scope, reason, retry_minutes: int) -> str` — `[{scope}] readiness command failed: {reason}. Keeping the last board; next try in {retry_minutes} min.`
  - `doc_missing_line(scope, doc, base) -> str` — `[{scope}] reviews are off: {doc} is not on {base}.`
- Produces (`jobs.py`):
  - `@dataclass(frozen=True) Notice(number: int, kind: str, key: str, text: str)`
  - `@dataclass Plan(queue: list[tuple[int, str, str, str]], states: list[tuple[str, str, str]], lines: list[str], notices: list[Notice])` — `queue` items `(number, kind, head_sha, key)`; `states` items `(job_id, state, reason)`
  - `plan_jobs(scope: str, update: BoardUpdate, jobs_by_pr: dict[int, list[Job]], is_skipped: Callable[[int, str], bool], cfg: ReviewerConfig, reviews_running: bool, now: datetime, first_poll: bool) -> Plan`
- Rules (`now` is aware UTC; times in rows are ISO strings):
  1. **Review.** For each row with `review` in its my-turn actions and `pipeline` false, key = `head_sha`:
     - every `queued` review job of this PR at another head → `superseded`, reason `new head`;
     - nothing more if a review job at this head exists in any state but `cancelled`, or `is_skipped(number, head)`;
     - else if `changed_files >= cfg.max_files_auto` → `Notice(number, "large", head, large_line(...))`;
     - else if `reviews_running` and `now - head_first_seen_at >= cfg.quiet_minutes` → queue `(number, "review", head, head)`, and every `done` review job of this PR at another head → `superseded`, reason `new head`.
  2. **Re-check.** For each row with `recheck` and without `review` in its my-turn actions, `pipeline` false: key = `f"{head}:{','.join(sorted(threads))}"` (threads of the recheck turn); queue `(number, "recheck", head, key)` when `reviews_running` and no recheck job with that key exists in any state but `cancelled`.
  3. **Gone turns.** For each `gone_turns` entry whose action is in `JOB_ACTIONS`: that kind's `queued` jobs of the PR → `cancelled`, reason `turn gone`; its `done` jobs → `stale`, reason `turn gone`.
  4. **Lines.** For each `new_turns` entry whose action is not in `JOB_ACTIONS`, whose row exists and is not `pipeline`, and only when not `first_poll`: `turn_line(...)`.
  5. The review, re-check and notice rules apply on the first poll too.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_reviewer_jobs.py
from __future__ import annotations
from datetime import datetime, timezone
from config.schemas import ReviewerConfig
from orchestrator.reviewer.board import BoardUpdate, TurnChange
from orchestrator.reviewer.jobs import Notice, plan_jobs
from orchestrator.reviewer.models import BoardRow, Job

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
OLD, RECENT = "2026-11-20T11:00:00+00:00", "2026-11-20T11:50:00+00:00"
A, B = "a" * 40, "b" * 40
CFG = ReviewerConfig(enabled=True, quiet_minutes=30, max_files_auto=40)

def row(n=1, actions=(("review", ()),), head=A, seen=OLD, files=14, pipeline=False):
    return BoardRow(n, "t", f"https://github.com/acme-org/acme-app/pull/{n}", "octo-author", ["reviewer"], head, seen,
                    False, files, [{"action": a, "threads": list(t)} for a, t in actions], [], False, pipeline, "",
                    "t", OLD)

def job(kind="review", head=A, state="queued", key=None, jid="j1"):
    return Job(jid, "acme/app", 1, kind, head, key or head, state, "", OLD, OLD)

def plan(rows, jobs=None, new=(), gone=(), running=True, first=False, skipped=lambda n, h: False):
    return plan_jobs("acme/app", BoardUpdate({r.number: r for r in rows}, list(new), list(gone)), jobs or {},
                     skipped, CFG, running, NOW, first)

def test_review_queued_after_quiet_window():
    assert plan([row()]).queue == [(1, "review", A, A)]
    assert plan([row(seen=RECENT)]).queue == []

def test_review_not_twice_and_skip_and_paused():
    assert plan([row()], {1: [job(state="done")]}).queue == []
    assert plan([row()], {1: [job(state="cancelled")]}).queue == [(1, "review", A, A)]
    assert plan([row()], skipped=lambda n, h: True).queue == []
    assert plan([row()], running=False).queue == []

def test_new_head_supersedes():
    p = plan([row(head=B)], {1: [job(head=A, state="queued", jid="q"), job(head=A, state="done", jid="d")]})
    assert p.queue == [(1, "review", B, B)]
    assert sorted(p.states) == [("d", "superseded", "new head"), ("q", "superseded", "new head")]

def test_large_pr_notice():
    p = plan([row(files=40)])
    assert p.queue == [] and p.notices == [Notice(1, "large", A,
        "[acme/app] #1 is large (40 files) — no automatic review · https://github.com/acme-org/acme-app/pull/1")]

def test_recheck_keyed_by_threads():
    r = row(actions=(("recheck", ("T2", "T1")),))
    assert plan([r]).queue == [(1, "recheck", A, f"{A}:T1,T2")]
    assert plan([r], {1: [job("recheck", key=f"{A}:T1,T2", state="done")]}).queue == []
    assert plan([row(actions=(("review", ()), ("recheck", ("T1",))))]).queue == [(1, "review", A, A)]

def test_gone_pr_cancels_queued():
    p = plan([], {1: [job(state="queued", jid="q"), job(state="done", jid="d")]},
             gone=[TurnChange(1, "review", ())])
    assert sorted(p.states) == [("d", "stale", "turn gone"), ("q", "cancelled", "turn gone")]

def test_turn_lines_and_first_poll_baseline():
    r = row(actions=(("merge", ()), ("answer_threads", ("T1", "T2", "T3"))))
    new = [TurnChange(1, "merge", ()), TurnChange(1, "answer_threads", ("T1", "T2", "T3"))]
    assert plan([r], new=new).lines == [
        "[acme/app] #1: your turn — merge · https://github.com/acme-org/acme-app/pull/1",
        "[acme/app] #1: replies in 3 thread(s) — handle them locally · https://github.com/acme-org/acme-app/pull/1"]
    assert plan([r], new=new, first=True).lines == []
    assert plan([row(actions=(("merge", ()),), pipeline=True)], new=new[:1]).lines == []

def test_first_poll_baseline():
    p = plan([row(), row(2, actions=(("merge", ()),))], new=[TurnChange(1, "review", ()), TurnChange(2, "merge", ())],
             first=True)
    assert p.queue == [(1, "review", A, A)] and p.lines == []
```

- [ ] **Step 2: Run to verify they fail** — `$PY -m pytest tests/unit/test_reviewer_jobs.py -q` → FAIL.
- [ ] **Step 3: Implement** `jobs.py` and `lines.py`.
- [ ] **Step 4: Run** — same command → all pass.

---

### Task 7: `ReviewerService` — the poll loop

**Files:**
- Create: `orchestrator/reviewer/service.py`
- Test: `tests/unit/test_reviewer_service.py`, `tests/integration/test_reviewer_poll.py`

**Interfaces:**
- Consumes: Tasks 1–6; `Switches.is_running("reviews", project, repo)` (plan 2); a `credential_failing(key: str) -> bool` callable (`CredentialHealth.failing`)
- Produces:
  - `@dataclass RepoContext(project_id: str, repo_id: str, cfg: ReviewerConfig, clone, readiness, forge)` with `scope` property → `f"{project_id}/{repo_id}"`; `clone` has `refresh()` and `has_file()`, `readiness` has `board(clone_dir)`, `forge` has `open_prs()`, `details()`, `reviewed_by()`; `clone.path` is the clone directory
  - `class ReviewerService(repos: list[RepoContext], store: ReviewerStore, switches, credential_failing: Callable[[str], bool], send_line: Callable[[str], Awaitable[Any]] | None, pipeline_prs: Callable[[str, str], set[int]], *, event_bus=None, now: Callable[[], datetime] = utcnow, max_backoff_seconds: int = 3600)`:
    - `async poll(ctx: RepoContext) -> None` — one poll of one repo (steps below); never raises
    - `async run(stop: asyncio.Event) -> None` — polls every repo whose `next_poll_at` is due (empty = due), sequentially, then waits until the earliest `next_poll_at` (at most 60 s) or `stop`
    - `review_now(scope: str, number: int) -> Job` — the row must exist (`LookupError`); `ValueError("already queued")` when a `queued` or `running` review job exists at the row's head; every `done` review job at that head → `superseded`, reason `new review`; then `add_job(scope, number, "review", head, head, now)`; ignores the quiet window, the large-PR rule, skips and the switch
    - `skip(scope: str, number: int) -> None` — the row must exist (`LookupError`); `store.skip(head)`; `queued` review jobs at that head → `skipped`, reason `skipped by operator`
    - `overview() -> list[dict]` — per repo: `{"scope", "status", "reason", "last_poll_at", "next_poll_at", "rows": [BoardRow...], "jobs": {number: latest Job}}` (the dashboard serialises it)
- Poll steps for one repo, `interval = cfg.poll_interval_seconds`:
  1. `credential_failing(f"forge:{scope}")` → status `token_failing`, reason `forge token failing`, `next_poll_at = now + interval`, stop (no line: credential health sends its own).
  2. `sha = await clone.refresh()`; `CloneError` → failure (step 7).
  3. `doc_ok = clone.has_file(cfg.process.doc)`; when false: send `doc_missing_line(scope, doc, base_branch)` once (notice kind `doc_missing`, number 0, key = doc) and continue with `reviews_running = False`; when true, `clear_notice(scope, 0, "doc_missing")`.
  4. `snapshot = await readiness.board(clone.path)`; `ReadinessError` → failure.
  5. `open_ = await forge.open_prs()`; for each snapshot PR in `open_`: `details` and `reviewed_by(number, snapshot.me, updated_at)`; `ForgeError` → failure.
  6. `update = build_board(...)` with `previous = store.board(scope)` and `pipeline_prs(project_id, repo_id)`; `plan = plan_jobs(..., reviews_running = doc_ok and switches.is_running("reviews", project_id, repo_id), first_poll = not state.first_poll_done)`; apply: `replace_board`, `set_job_state` for `plan.states`, `add_job` for `plan.queue`, send `plan.lines`, send each notice not yet `notice_sent` and `mark_notice` it; status `doc_missing` (reason `"{doc} not on {base}"`) or `ok`; `first_poll_done = True`, `failures = 0`, `last_ok_at = now`, `next_poll_at = now + interval`.
  7. Failure: `failures += 1`; `delay = min(interval * 2 ** failures, max_backoff_seconds)`; status `readiness_failed` (readiness) or `error` (clone/forge), reason = the error text; the board is kept; on the first failure of a streak (`failures == 1`) send `readiness_failed_line(scope, reason, delay // 60)` for readiness failures (clone/forge failures: same line text with `readiness command failed` replaced by `poll failed`); `next_poll_at = now + delay`.
  - Every step saves `RepoState` with `last_poll_at = now`. Events on the bus (when given): `reviewer_poll` (data `{"scope", "prs": len(rows)}`), `reviewer_poll_failed` (`{"scope", "reason"}`), `reviewer_turn` per new turn (`{"scope", "number", "action"}`), `reviewer_job` per queued or changed job (`{"scope", "number", "kind", "state"}`).
  - A failing `send_line` is logged with the exception class only; it never fails the poll.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_reviewer_service.py`, fakes for clone, readiness and forge; real `ReviewerStore` and `Switches` on `tmp_path`)

```python
class FakeClone:
    def __init__(self, path, doc=True, fail=None): self.path, self.doc, self.fail = path, doc, fail
    async def refresh(self):
        if self.fail: raise self.fail
        return "c" * 40
    def has_file(self, rel): return self.doc

class FakeReadiness:
    def __init__(self, snapshot=None, fail=None): self.snapshot, self.fail = snapshot, fail
    async def board(self, clone_dir):
        if self.fail: raise self.fail
        return self.snapshot

class FakeForge:
    def __init__(self, details): self.d = details
    async def open_prs(self): return {n: "t" for n in self.d}
    async def details(self, n, u): return self.d[n]
    async def reviewed_by(self, n, login, u): return False
```

Tests (names and assertions; build the snapshot/details with the helpers from Task 5's tests):
- `test_poll_builds_board_and_queues_review` — a review turn, head seen > 30 min ago on a previous poll → after two polls (first records `head_first_seen_at = T0`, clock advanced 31 min) one `queued` review job; `store.repo_state(scope).status == "ok"`; `first_poll_done`.
- `test_first_poll_sends_no_turn_lines` — first poll with a `merge` turn → `send_line` not called; second poll after the turn disappeared and reappeared → one line `"[acme/app] #2: your turn — merge · u2"`.
- `test_token_failing_skips_poll` — `credential_failing` returns True → clone, readiness and forge never called; status `token_failing`; `next_poll_at` = now + 300 s.
- `test_readiness_failure_keeps_board_and_backs_off` — a good poll, then `ReadinessError("exit 3: boom")` twice → board unchanged; status `readiness_failed`; reason `exit 3: boom`; exactly one line `"[acme/app] readiness command failed: exit 3: boom. Keeping the last board; next try in 10 min."` (failures 1 → delay 600 s); after the second failure `next_poll_at` = now + 1200 s.
- `test_doc_missing_no_jobs_one_line` — `FakeClone(doc=False)` with a review turn past the quiet window → no job; status `doc_missing`; one line `"[acme/app] reviews are off: docs/review/code-review-process.md is not on develop."` over two polls.
- `test_reviews_paused_no_jobs_but_board` — reviews switch paused for the repo → board rows present, no job.
- `test_review_now_and_skip` — `review_now` queues ignoring the quiet window; a second call raises `ValueError`; `skip` turns that queued job into `skipped` and marks the head skipped; unknown PR raises `LookupError`.
- `test_failing_repo_does_not_block_others` — two repos, the first's readiness raises; `run(stop)` with `stop` set after one round polls both (second repo status `ok`).
- `test_send_line_failure_is_harmless` — `send_line` raises → poll completes, status `ok`.
- `tests/integration/test_reviewer_poll.py::test_poll_end_to_end` — real `ReviewClone` on a local bare repo (Task 3 fixture pattern) holding `docs/review/code-review-process.md` and an executable `tools/status.sh` that prints a board snapshot; real `CommandReadiness` with `status_command: ["sh", "tools/status.sh"]`; `FakeForge`; one poll → one board row, status `ok`, the clone detached at origin's head.

- [ ] **Step 2: Run to verify they fail** — `$PY -m pytest tests/unit/test_reviewer_service.py tests/integration/test_reviewer_poll.py -q` → FAIL.
- [ ] **Step 3: Implement** `service.py`.
- [ ] **Step 4: Run** — same command → all pass; then the suite → no new failures.

---

### Task 8: Dashboard — the Reviews tab

**Files:**
- Create: `dashboard/reviews_api.py`, `dashboard/static/js/reviews.js`
- Modify: `dashboard/web.py` (`create_app(..., reviewer=None)`; routes registered when given), `dashboard/static/index.html` (nav link `<a class="nav-link" id="nav-reviews">Reviews</a>` after Board), `dashboard/static/js/app.js` (`showReviews(fromHash)`, hash `#/reviews`, `routeFromHash` and auto-refresh handle `reviews`), `dashboard/static/js/api.js` (`loadReviews()`, `reviewNow(scope, number)`, `skipReview(scope, number)`), `dashboard/static/style.css`
- Test: `tests/unit/test_dashboard_reviews.py`

**Interfaces:**
- Consumes: `ReviewerService.overview()`, `.review_now()`, `.skip()` (Task 7); `renderSwitchPanel(container, 'reviews')` (plan 2)
- Produces:
  - `build_review_routes(service) -> list[Route]`
  - `GET /api/reviews` → `{"repos": [{"scope", "status", "reason", "last_poll_at", "next_poll_at", "my_turn": [PR], "waiting": [PR]}]}`; PR = `{"number", "title", "url", "ticket_key", "roles", "my_turns": [action], "other_turns": [{"login", "action"}], "pipeline", "draft", "changed_files", "head": head_sha[:9], "job": {"id", "kind", "state", "head": head_sha[:9]} | null, "last_change"}`; `my_turn` = rows with my-turn actions and not `pipeline`, ordered by number descending; `waiting` = the rest
  - `POST /api/reviews/review-now` body `{"scope", "number"}` → 200 `{"job": {...}}`; 404 `{"error"}` for `LookupError`; 409 `{"error"}` for `ValueError`; 400 for a bad body
  - `POST /api/reviews/skip` body `{"scope", "number"}` → 200 `{"ok": true}`; 404 / 400 as above
  - Tab layout: the reviews switch panel on top; per repo a header `<scope> · <status>` (and the reason when not `ok`, e.g. `reviews are off: …`), then two tables "My turn" and "Waiting on others": `#n` linked to the PR, title, ticket key, roles, turns (my actions in bold, others as `login: action`), a `pipeline` badge, job state, `last change` age; buttons **Review now** (every row) and **Skip** (rows with a `review` turn) that call the API and re-render; every interpolated value through `esc()`
  - No posting from the dashboard (spec, Security); no detail view (plan 4)

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_dashboard_reviews.py` with a `FakeService` (`overview`, `review_now`, `skip` recording calls):
  - `test_get_reviews_groups_and_trims` — one row with a `review` turn, one `pipeline` row with `answer_threads`, one with only other turns → `my_turn` numbers `[1]`, `waiting` sorted `[3, 2]`; `head` and `job.head` are 9 chars.
  - `test_review_now_codes` — 200 with the job; `LookupError` → 404; `ValueError("already queued")` → 409 with `"already queued"`; body `{}` → 400.
  - `test_skip_codes` — 200 `{"ok": true}`; `LookupError` → 404.
  - `test_routes_absent_without_service` — `GET /api/reviews` → 404 when `create_app` gets no `reviewer`.
- [ ] **Step 2: Run to verify they fail** — `$PY -m pytest tests/unit/test_dashboard_reviews.py -q` → FAIL.
- [ ] **Step 3: Implement** the routes, `reviews.js`, nav and routing, CSS (reuse the Board's table and badge styles).
- [ ] **Step 4: Run** — `$PY -m pytest tests/unit/test_dashboard_reviews.py tests/unit/test_dashboard_web.py tests/unit/test_dashboard_switches.py -q` → all pass; `node --check dashboard/static/js/reviews.js` → no output. The tab is checked in a browser in Task 9.

---

### Task 9: Wiring, docs, smoke run

**Files:**
- Modify: `main.py`, `docs/dashboard.md` (Reviews tab), `docs/telegram.md` (turn lines, large-PR line, readiness failure line), `README.md` only if it lists dashboard tabs
- Test: `tests/unit/test_main_reviewer.py` (new), the suite, a smoke run

**Interfaces:**
- Consumes: everything above; plan 2's `reviewer_db_path`, `connect`, `Switches`, `CredentialHealth.failing`, `operator_chat_id`; the per-repo `GitHubAdapter` already built in `main.py` (`vcs_adapters[repo_id][0]`)
- Produces in `main.py`:
  - `_reviewer_contexts(global_config, projects, vcs_adapters) -> list[RepoContext]` (module level, testable): one context per loaded repo with `reviewer.enabled`; `clone = ReviewClone(reviewer_db_path(global_config.reviewer).parent / "repos" / repo_id, github_clone_url(owner, repo), cfg.base_branch, vcs.github.token)`; `readiness = CommandReadiness(cfg.readiness, repo=f"{owner}/{repo}", token=vcs.github.token)`; `forge = GitHubForgeReader(adapter)`
  - when the list is not empty: `ReviewerService(contexts, ReviewerStore(connect(db_path)), switches, credential_health.failing, send_line, pipeline_prs, event_bus=event_bus)`; `send_line` sends to `global_config.reviewer.telegram_chat_id or operator_chat_id(...)` through the notifier when there is one; `pipeline_prs(project_id, repo_id)` = the `pr_number`s of `orchestrator.get_active_workspaces()` whose `company_id`/`repo_id` match
  - `create_app(..., reviewer=service)`; in `_run_all` the service's `run(stop)` starts as a task next to the credential health task and stops the same way; print `  Reviewer: <n> repo(s)` (or nothing when there are none)
  - repos added by hot reload are not picked up by the reviewer until a restart (docs say so)

- [ ] **Step 1: Write the failing test** — `tests/unit/test_main_reviewer.py::test_reviewer_contexts` — two loaded repos, one with `reviewer.enabled` (GitHub `acme-org/acme-app`, token `tok`, base `develop`) and a fake adapter → one context; `ctx.scope == "acme/app"`; `ctx.clone.path == <tmp db dir>/repos/app`; `ctx.clone.url == "https://github.com/acme-org/acme-app.git"`; the readiness part's `repo == "acme-org/acme-app"`; no token in `repr(ctx)`.
- [ ] **Step 2: Run to verify it fails**, **Step 3: implement**, **Step 4: run** — `$PY -m pytest tests/unit/test_main_reviewer.py -q`, then the suite → no new failures.
- [ ] **Step 5: Smoke run** — a scratch config like plan 2's smoke (`os: mac`, dashboard on 127.0.0.1:18765, `cap_build_memory: false`, scratch `reviewer.db_path`, logs, workspaces) plus one project with one repo: `vcs.github` owner `acme-org`, repo `acme-app`, token `ghp_smoke_not_real`, and the spec's `reviewer:` block with `enabled: true`. `PATH` = a fake `claude` dir first, then `/usr/bin:/bin`. Start the daemon; after ≤ 60 s:
  Run: `curl -s 127.0.0.1:18765/api/reviews`
  Expected: one repo `acme/acme-app` with a status other than `new` and a non-empty reason (with a fake token: `token_failing` once credential health has checked, or `error` from the clone); no traceback in the log; `ghp_smoke_not_real` appears nowhere in the log or in `"$SMOKE"/rv` (`grep -r`). Open `http://127.0.0.1:18765/#/reviews` in a browser: the Reviews nav entry, the reviews switch panel, the repo header with its status. Stop the daemon (SIGTERM, exits ≤ 10 s); delete the scratch directory.
- [ ] **Step 6: Leak check** — `grep -rn -i -E -e "$(grep -v -E '^(#|$)' ~/.config/cleave/denylist.txt)" -- config config-live.example dashboard docs health integrations orchestrator tests main.py README.md; echo "exit=$?"` → `exit=1`.
- [ ] **Step 7: Hand back to the operator**; commit only if asked.

## Deployment notes for plan 6

- The first poll of the live repo needs the new token (H4) and gh on the daemon PATH; until then the repo shows `token_failing`.
- The review clone is created at `~/CleaveData/reviewer/repos/<project_id>/<repo_id>` on the first poll (a full single-branch clone of the base branch).
- H5 step 3 (a clone with the env-passed token, no keychain dialog) is exactly `ReviewClone.refresh()`; run it once under launchd before enabling the daemon.
- The threads tool's own tests (H7) run on that clone.
