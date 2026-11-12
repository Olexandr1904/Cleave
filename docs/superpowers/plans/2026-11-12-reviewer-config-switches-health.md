# Reviewer Mode — Config, Switches, Credential Health (Plan 2 of 6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse reviewer configuration, add persistent running/paused switches for tickets and reviews (dashboard + Telegram), make paused tickets really stop, and show every credential's health on the dashboard and in Telegram — including an expired token or `claude` login.

**Architecture:** Config gains a per-repo `reviewer:` section, a global `reviewer:` section, the three global sections the loader silently dropped, and a record of which `${VAR}` each field came from. A `Switches` store lives in `reviewer.db` (SQLite, outside `workspaces.base_dir`) and gates `ingest` and `runtime` for tickets; reviews switches are only stored and shown here (consumed from plan 3). A `CredentialHealth` service checks every credential every 15 minutes and on any adapter 401/403, keeps per-credential state, sends Telegram lines on change, and feeds a new `/api/health/credentials` endpoint that replaces the on-demand `/api/projects/health`.

**Tech Stack:** Python 3.12, dataclasses, `sqlite3`, asyncio, httpx + respx, Starlette, python-telegram-bot (unchanged), vanilla JS modules in `dashboard/static/js/`.

**Spec:** `docs/superpowers/specs/2026-11-12-reviewer-mode-design.md` — sections "Switches", "Credential health", "State", "Configuration", "Constraints", "Security notes". Host-check results that shape this plan: `docs/superpowers/plans/2026-11-12-reviewer-host-checks.md` ("Results", "Consequences for plans 2–6").

**Later plans:** 3 — board (forge reader, readiness part, reviewer task, turn lines, Reviews tab, which mounts the reviews switch panel and consumes `Switches.is_running("reviews", …)` and `CredentialHealth.failing("forge:…")`); 4 — review job (consumes `failing("claude_login")`, reports run login failures through `report_auth_failure("claude_login", 401)`); 5 — posting; 6 — deployment.

## Global Constraints

- No employer names anywhere: examples use `acme`, `acme-org/acme-app`, `ACME-1234`.
- A token value never appears in a log line, a `reason`, an API response, a Telegram line or a test assertion message — only the environment variable's **name** (spec: "the name, never the value"). The Telegram Bot API URL carries the token: never log or return an httpx error's text for it, only the exception class name.
- Tests never start the real `claude`: every test passes a fake script as `claude_bin`; only `main.py` resolves the real one (`shutil.which("claude")`).
- Every `claude` subprocess gets an environment of exactly `HOME`, `PATH`, `LANG`, `TMPDIR`, `USER` copied from `os.environ` (when set) — no API key reaches it (operator's rule: subscription only).
- New code runs subprocesses with `asyncio.create_subprocess_exec` only (spec, "Blocking calls").
- Reviewer state lives in `reviewer.db`, default `${HOME}/CleaveData/reviewer/reviewer.db`, never under `workspaces.base_dir`, never named `state.json` (spec, "State"). Nothing expands `~`.
- Switch states are exactly `running` / `paused`; modes exactly `tickets` / `reviews`; first boot: tickets `paused`, reviews `running` (spec, "Switches").
- Credential states are exactly `unknown`, `ok`, `expiring`, `invalid`, `no_access`, `unreachable`; background interval 900 s; `claude` login `expiring` within 3 days; warnings 3 days and 1 day before `loginExpiresAt`; nothing is sent to Telegram about the Telegram token itself (spec, "Credential health").
- Telegram fixed commands (spec): «ревью пауза», «ревью старт», «тикеты пауза», «тикеты старт», optionally + a repo id, and «статус»; they run before the LLM intent parser and only for allowlisted chats.
- `PY` below is a Python 3.12 interpreter with `pip install -e '.[dev]'`; `WS` is this plan's git-ignored workspace, `.superpowers/sdd/2026-11-12-reviewer-config-switches-health/`. Suite command: `$PY -m pytest tests/unit tests/integration -q`. `tests/e2e` needs playwright and is not run here.
- Work on branch `reviewer-config-switches`, created with `git switch -c` off `master`. No commit and no push unless the operator asks in that message; when asked, group the work into logical commits (operator's rule) — the tasks below have no commit steps.

## Review Focus

- **Token values leaking through the new surfaces** — the Telegram `getMe` URL contains the bot token and httpx puts URLs in error text; the credentials API and snapshot carry labels and variable names. Expected: no token value anywhere. Pinned by `test_check_telegram_network_error_hides_token` (Task 7) and `test_snapshot_never_contains_token_values` (Task 8).
- **Restarts** — the operator resumes tickets once; a daemon restart must not re-pause them, and first-boot defaults must be written exactly once. Pinned by `test_first_boot_defaults_written_once` (Task 3) and the restart step of the smoke run (Task 11).
- **A 401 storm** — a revoked token makes every adapter call fail; each 401 must not start its own re-check. Expected: one re-check per credential per 60 s. Pinned by `test_report_auth_failure_rechecks_once_within_gap` (Task 8).
- **`claude auth status` hanging or printing something unexpected** (keychain locked, CLI update) — must not block the health loop or crash it. Expected: `unreachable` after 30 s, loop continues. Pinned by `test_check_claude_login_timeout_kills_and_reports` and `test_check_claude_login_non_json` (Task 7).
- **The host's live `global.yaml`** — `pipeline`, `intent_parser` and `dashboard` start being parsed; an unknown field there now stops the daemon at start. Expected: a `ConfigError` that names the section. Pinned by `test_unknown_dashboard_field_names_the_section` (Task 1); the host's file is checked at deployment (plan 6).

---

### Task 0: Branch and baseline

- [ ] **Step 1: Branch off master**

Run: `git branch --show-current && git switch -c reviewer-config-switches`
Expected: first line `master`; then `Switched to a new branch 'reviewer-config-switches'`. The untracked docs stay in the working tree.

- [ ] **Step 2: Baseline suite**

Run: `$PY -m pytest tests/unit tests/integration -q > $WS/baseline.log 2>&1; tail -3 $WS/baseline.log`
Expected: `1351 passed, 9 skipped` (the count after the history scrub). Any other count: record it in the ledger as the baseline and continue.

---

### Task 1: Global config — dropped sections, `reviewer:` globals, env-var names

**Files:**
- Modify: `config/schemas.py` (new `ReviewerJobConfig`, `ReviewerGlobalConfig`; `env_refs` on `GlobalConfig`, `ProjectConfig`, `RepoConfig`)
- Modify: `config/config_loader.py:168-178` (`load_global_config`), `_load_yaml_file`, `_load_project_config`, `_load_repo_config`
- Test: `tests/unit/test_config_reviewer.py` (new)

**Interfaces:**
- Produces:
  - `ReviewerJobConfig(model: str = "", effort: str = "", timeout_seconds: int = 0)`
  - `ReviewerGlobalConfig(db_path: str = "", telegram_chat_id: str = "", jobs: dict[str, ReviewerJobConfig])`, `jobs` default:
    `review`=(`opus`, `xhigh`, 3600), `recheck`=(`opus`, `xhigh`, 3600), `edit`=(`sonnet`, `""`, 300), `draft`=(`sonnet`, `""`, 300), `command`=(`sonnet`, `""`, 60)
  - `GlobalConfig.reviewer: ReviewerGlobalConfig`; `GlobalConfig.env_refs`, `ProjectConfig.env_refs`, `RepoConfig.env_refs`: `dict[str, str]` — dotted YAML path → the first `${VAR}` name in that string (`"vcs.github.token" → "GITHUB_TOKEN"`), computed from the raw YAML before resolution
  - `env_var_refs(raw: Any, prefix: str = "") -> dict[str, str]` in `config/config_loader.py` (dicts by key, lists by index)
  - `_load_yaml_file(path: Path) -> tuple[dict, dict[str, str]]` (resolved data, env refs)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_config_reviewer.py
from __future__ import annotations
import pytest
from config.config_loader import ConfigError, load_config, load_global_config

def _global(tmp_path, text):
    (tmp_path / "global.yaml").write_text(text)
    return load_global_config(str(tmp_path))

def test_global_sections_pipeline_intent_dashboard_parsed(tmp_path):
    cfg = _global(tmp_path, "pipeline:\n  mode: auto\nintent_parser:\n  timeout_seconds: 7\n"
                            "dashboard:\n  host: 127.0.0.1\n  port: 9191\n")
    assert cfg.pipeline.mode == "auto"
    assert cfg.intent_parser.timeout_seconds == 7
    assert (cfg.dashboard.host, cfg.dashboard.port) == ("127.0.0.1", 9191)

def test_unknown_dashboard_field_names_the_section(tmp_path):
    with pytest.raises(ConfigError, match="dashboard"):
        _global(tmp_path, "dashboard:\n  colour: red\n")

def test_reviewer_global_defaults(tmp_path):
    r = _global(tmp_path, "os: mac\n").reviewer
    assert (r.db_path, r.telegram_chat_id) == ("", "")
    assert (r.jobs["review"].model, r.jobs["review"].effort, r.jobs["review"].timeout_seconds) == ("opus", "xhigh", 3600)
    assert (r.jobs["recheck"].model, r.jobs["recheck"].effort) == ("opus", "xhigh")
    assert (r.jobs["edit"].model, r.jobs["edit"].timeout_seconds) == ("sonnet", 300)
    assert (r.jobs["command"].model, r.jobs["command"].timeout_seconds) == ("sonnet", 60)

def test_reviewer_global_job_override_merges(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/h")
    r = _global(tmp_path, 'reviewer:\n  db_path: "${HOME}/x/reviewer.db"\n'
                          "  jobs:\n    review:\n      timeout_seconds: 5400\n").reviewer
    assert r.db_path == "/h/x/reviewer.db"
    assert (r.jobs["review"].model, r.jobs["review"].effort, r.jobs["review"].timeout_seconds) == ("opus", "xhigh", 5400)

def test_reviewer_global_unknown_job_kind_raises(tmp_path):
    with pytest.raises(ConfigError, match="reviewer.jobs"):
        _global(tmp_path, "reviewer:\n  jobs:\n    deploy:\n      model: opus\n")

def test_env_refs_record_variable_names(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_T", "secret-1")
    monkeypatch.setenv("GH_T", "secret-2")
    monkeypatch.setenv("JI_T", "secret-3")
    (tmp_path / "global.yaml").write_text('telegram:\n  bot_token: "${BOT_T}"\n')
    pdir = tmp_path / "projects" / "p1"
    (pdir / "repos").mkdir(parents=True)
    (pdir / "project.yaml").write_text(
        'project:\n  id: p1\ntracker:\n  provider: jira\n  jira:\n    url: https://x\n    token: "${JI_T}"\n')
    (pdir / "repos" / "r1.yaml").write_text(
        'vcs:\n  provider: github\n  github:\n    token: "${GH_T}"\n    owner: o\n    repo: r\n')
    g, projects = load_config(str(tmp_path))
    assert g.env_refs["telegram.bot_token"] == "BOT_T"
    assert projects["p1"].config.env_refs["tracker.jira.token"] == "JI_T"
    assert projects["p1"].repos["r1"].env_refs["vcs.github.token"] == "GH_T"
    everything = repr(g.env_refs) + repr(projects["p1"].config.env_refs) + repr(projects["p1"].repos["r1"].env_refs)
    assert "secret" not in everything
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_config_reviewer.py -q`
Expected: FAIL — `pipeline.mode` is `manual` (section dropped), `AttributeError: 'GlobalConfig' object has no attribute 'reviewer'`, no `env_refs`.

- [ ] **Step 3: Implement**

`load_global_config` adds `pipeline`, `intent_parser`, `dashboard` via `_parse_section`, `reviewer=_parse_reviewer_global(data, file_str)`, `env_refs=env_var_refs(raw_data)`. `_parse_reviewer_global` merges each YAML job entry over the default of the same kind (fields not given keep the default); an unknown kind or field raises `ConfigError(field="reviewer.jobs.<kind>")`. `_load_yaml_file` returns `(resolved, env_var_refs(raw))`; both callers store the refs on the dataclass they build.

- [ ] **Step 4: Run the tests and the existing config tests**

Run: `$PY -m pytest tests/unit/test_config_reviewer.py tests/unit/test_config_loader.py tests/unit/test_config_cascade.py tests/unit/test_tracker_config_loader.py -q`
Expected: all pass. If a fixture `global.yaml` under `tests/fixtures/` now fails because one of the three sections carries an unknown field, fix the fixture rather than the loader, and ledger it.

---

### Task 2: Per-repo `reviewer:` section

**Files:**
- Modify: `config/schemas.py` (new `ReadinessPartConfig`, `ProcessPartConfig`, `PosterPartConfig`, `ReviewerConfig`, `REVIEWER_PLACEHOLDERS`; `RepoConfig.reviewer`)
- Modify: `config/config_loader.py` (`_parse_reviewer_section`, called from `_load_repo_config` after the `vcs` section)
- Modify: `config-live.example/projects/example-project/repos/example-repo.yaml` (add the spec's `reviewer:` block with `enabled: false`), `config-live.example/global.yaml` (add a commented `reviewer:` block showing `db_path: "${HOME}/CleaveData/reviewer/reviewer.db"` and one `jobs` override)
- Test: `tests/unit/test_config_reviewer.py` (extend)

**Interfaces:**
- Consumes: `VCSConfig` (provider, default_branch)
- Produces:
  - `ReadinessPartConfig(kind="command", status_command: list[str] = [], threads_command: list[str] = [])`
  - `ProcessPartConfig(doc="", prepare_section="Prepare", recheck_section="Re-check", run_dir="", replies_plan="", worktree_dir="", prepare_steps: list[list[str]] = [], helper_scripts: list[str] = [])`
  - `PosterPartConfig(kind="command", review_command: list[str] = [], replies_command: list[str] = [], dry_run_flag="--dry-run", confirm_flag="--yes")`
  - `ReviewerConfig(enabled=False, base_branch="", poll_interval_seconds=300, quiet_minutes=30, max_files_auto=40, readiness, process, poster)`; after load `base_branch` is never empty (defaults to `vcs.default_branch`)
  - `REVIEWER_PLACEHOLDERS = frozenset({"repo", "pr", "head9", "worktree", "merge_base", "report", "plan", "job_dir", "ledger", "event"})` (plans 3–5 expand exactly these)
  - `_parse_reviewer_section(data: dict, vcs: VCSConfig, file_path: str) -> ReviewerConfig`

Validation runs only when `enabled: true` (field names are checked always, by the dataclass constructors):
1. `vcs.provider == "github"`, else `ConfigError` mentioning "GitHub" (only the GitHub forge reader is built).
2. `readiness.kind` and `poster.kind` are `"command"`, else `ConfigError` "... kind '<k>' is not built in this version".
3. Non-empty: `readiness.status_command`, `readiness.threads_command`, `process.doc`, `process.run_dir`, `process.replies_plan`, `process.worktree_dir`, `poster.review_command`, `poster.replies_command`, `poster.dry_run_flag`, `poster.confirm_flag` — error field `reviewer.<part>.<name>`.
4. Every command is a list of strings (`prepare_steps`: a list of such lists), else `ConfigError` "... must be a list".
5. `process.doc`, `process.run_dir`, `process.replies_plan` are repo-relative: not absolute, no `..` part, else `ConfigError` "... must be repo-relative".
6. Every `{name}` in any command string and in `run_dir`, `replies_plan`, `worktree_dir` is in `REVIEWER_PLACEHOLDERS`, else `ConfigError` naming it.

- [ ] **Step 1: Write the failing tests** (append)

```python
from pathlib import Path
import yaml
from config.config_loader import _parse_reviewer_section
from config.schemas import VCSConfig

FULL = """
reviewer:
  enabled: true
  base_branch: develop
  readiness:
    kind: command
    status_command: ["python3", "tools/threads/threads.py", "--repo", "{repo}", "status"]
    threads_command: ["python3", "tools/threads/threads.py", "--repo", "{repo}", "threads", "{pr}"]
  process:
    doc: docs/review/code-review-process.md
    run_dir: "tools/review/runs/PR_{pr}"
    replies_plan: "tools/threads/runs/PR_{pr}/replies_{head9}.json"
    worktree_dir: "../review-wt/PR_{pr}"
    prepare_steps:
      - ["python3", "tools/review/anchors.py", "{pr}", "--worktree", "{worktree}", "--base", "{merge_base}"]
    helper_scripts: ["tools/review/*.py"]
  poster:
    kind: command
    review_command: ["python3", "tools/review/post.py", "{pr}", "--repo", "{repo}", "--report", "{report}",
                     "--anchors", "{job_dir}/anchors.json", "--ledger", "{ledger}", "--event", "{event}"]
    replies_command: ["python3", "tools/threads/threads.py", "--repo", "{repo}", "post", "{pr}", "--plan", "{plan}"]
"""

def _section(text):
    return yaml.safe_load(text)

def _vcs(provider="github"):
    return VCSConfig(provider=provider, default_branch="main")

def _with(path_and_value):
    data = _section(FULL)
    node = data["reviewer"]
    *parents, leaf = path_and_value[0].split(".")
    for p in parents:
        node = node[p]
    node[leaf] = path_and_value[1]
    return data

def test_reviewer_section_parsed():
    r = _parse_reviewer_section(_section(FULL), _vcs(), "r.yaml")
    assert r.enabled and r.base_branch == "develop"
    assert (r.poll_interval_seconds, r.quiet_minutes, r.max_files_auto) == (300, 30, 40)
    assert r.readiness.status_command[-1] == "status"
    assert r.process.prepare_section == "Prepare" and r.process.recheck_section == "Re-check"
    assert r.process.prepare_steps[0][1] == "tools/review/anchors.py"
    assert (r.poster.dry_run_flag, r.poster.confirm_flag) == ("--dry-run", "--yes")

def test_reviewer_absent_defaults_disabled_with_vcs_branch():
    r = _parse_reviewer_section({}, _vcs(), "r.yaml")
    assert not r.enabled and r.base_branch == "main"

def test_reviewer_disabled_partial_section_loads():
    r = _parse_reviewer_section({"reviewer": {"enabled": False, "readiness": {"kind": "rule"}}}, _vcs(), "r.yaml")
    assert not r.enabled

def test_reviewer_unknown_field_raises_even_when_disabled():
    with pytest.raises(ConfigError, match="reviewer"):
        _parse_reviewer_section({"reviewer": {"enabled": False, "quiet_minuts": 5}}, _vcs(), "r.yaml")

def test_reviewer_on_gitlab_repo_raises():
    with pytest.raises(ConfigError, match="GitHub"):
        _parse_reviewer_section(_section(FULL), _vcs("gitlab"), "r.yaml")

def test_reviewer_rule_kind_raises():
    with pytest.raises(ConfigError, match="not built"):
        _parse_reviewer_section(_with(("readiness.kind", "rule")), _vcs(), "r.yaml")

def test_reviewer_missing_doc_raises():
    with pytest.raises(ConfigError, match="reviewer.process.doc"):
        _parse_reviewer_section(_with(("process.doc", "")), _vcs(), "r.yaml")

def test_reviewer_doc_outside_repo_raises():
    with pytest.raises(ConfigError, match="repo-relative"):
        _parse_reviewer_section(_with(("process.doc", "../x.md")), _vcs(), "r.yaml")

def test_reviewer_unknown_placeholder_raises():
    with pytest.raises(ConfigError, match="prr"):
        _parse_reviewer_section(_with(("readiness.status_command", ["python3", "t.py", "{prr}"])), _vcs(), "r.yaml")

def test_reviewer_command_must_be_list():
    with pytest.raises(ConfigError, match="list"):
        _parse_reviewer_section(_with(("readiness.status_command", "python3 t.py status")), _vcs(), "r.yaml")

def test_example_repo_reviewer_block_validates_when_enabled():
    path = Path("config-live.example/projects/example-project/repos/example-repo.yaml")
    data = yaml.safe_load(path.read_text())
    assert data["reviewer"]["enabled"] is False
    data["reviewer"]["enabled"] = True
    assert _parse_reviewer_section(data, _vcs(), str(path)).enabled
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_config_reviewer.py -q`
Expected: FAIL — `ImportError: cannot import name '_parse_reviewer_section'`.

- [ ] **Step 3: Implement** the dataclasses, `_parse_reviewer_section`, the call in `_load_repo_config`, and the two example blocks (neutral paths copied from the spec's "Configuration" example).

- [ ] **Step 4: Run**

Run: `$PY -m pytest tests/unit/test_config_reviewer.py tests/unit/test_config_loader.py tests/unit/test_config_cascade.py -q`
Expected: all pass.

---

### Task 3: `reviewer.db` and the `Switches` store

**Files:**
- Create: `orchestrator/reviewer/__init__.py` (empty), `orchestrator/reviewer/db.py`
- Create: `orchestrator/switches.py`
- Test: `tests/unit/test_switches.py`

**Interfaces:**
- Consumes: `ReviewerGlobalConfig.db_path` (Task 1), `ReviewerConfig.enabled` (Task 2), `LoadedProject`
- Produces:
  - `reviewer_db_path(cfg: ReviewerGlobalConfig) -> Path` — `Path(cfg.db_path)` when set, else `Path.home() / "CleaveData" / "reviewer" / "reviewer.db"`
  - `MIGRATIONS: list[str]` — entry *i* brings `PRAGMA user_version` to *i+1*; this plan adds entry 1:
    `CREATE TABLE switches (mode TEXT NOT NULL, scope TEXT NOT NULL, state TEXT NOT NULL, changed_at TEXT NOT NULL, changed_by TEXT NOT NULL, PRIMARY KEY (mode, scope))`
  - `connect(path: Path) -> sqlite3.Connection` — creates the parent directory (mode 0700), applies pending migrations in one transaction each, returns the connection (plans 3–5 append migrations)
  - `MODES = ("tickets", "reviews")`, `STATES = ("running", "paused")`, `GLOBAL = "*"`, `FIRST_BOOT = {"tickets": "paused", "reviews": "running"}`
  - `scope_of(project_id: str, repo_id: str) -> str` → `"<project_id>/<repo_id>"`
  - `switch_scopes(projects: dict[str, LoadedProject]) -> dict[str, list[str]]` → `{"tickets": [every loaded repo], "reviews": [repos with reviewer.enabled]}`, each sorted
  - `class Switches(conn: sqlite3.Connection)`:
    - `get(mode, scope=GLOBAL) -> str` (a repo with no row → `"running"`)
    - `set(mode, state, scope=GLOBAL, by="dashboard") -> None` (`ValueError` on unknown mode/state; persists; then calls every listener; a raising listener is logged, never propagated)
    - `is_running(mode, project_id, repo_id) -> bool` (global running **and** repo running)
    - `add_listener(fn: Callable[[str, str, str, str], None]) -> None` — `fn(mode, scope, state, by)`
    - `snapshot(scopes: dict[str, list[str]]) -> dict` →
      `{"tickets": {"global": "paused", "repos": [{"scope": "p/r", "state": "running", "effective": "paused"}]}, "reviews": {...}}`
  - On construction, a missing global row for a mode is inserted from `FIRST_BOOT` with `changed_by="boot"`; existing rows are never overwritten.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_switches.py
from __future__ import annotations
from pathlib import Path
import pytest
from config.schemas import LoadedProject, RepoConfig, ReviewerConfig, ReviewerGlobalConfig
from orchestrator.reviewer.db import MIGRATIONS, connect, reviewer_db_path
from orchestrator.switches import Switches, switch_scopes

@pytest.fixture
def db(tmp_path):
    return tmp_path / "rv" / "reviewer.db"

def test_first_boot_defaults(db):
    s = Switches(connect(db))
    assert (s.get("tickets"), s.get("reviews")) == ("paused", "running")

def test_first_boot_defaults_written_once(db):
    Switches(connect(db)).set("tickets", "running", by="telegram")
    assert Switches(connect(db)).get("tickets") == "running"

def test_effective_state_paused_if_either(db):
    s = Switches(connect(db))
    s.set("reviews", "paused", scope="p/r")
    assert not s.is_running("reviews", "p", "r")
    s.set("reviews", "running", scope="p/r")
    s.set("reviews", "paused")
    assert not s.is_running("reviews", "p", "r")
    s.set("reviews", "running")
    assert s.is_running("reviews", "p", "r")

def test_repo_without_row_inherits_global(db):
    s = Switches(connect(db))
    assert s.is_running("reviews", "p", "new") and not s.is_running("tickets", "p", "new")

def test_set_rejects_unknown_mode_and_state(db):
    s = Switches(connect(db))
    with pytest.raises(ValueError):
        s.set("deploys", "running")
    with pytest.raises(ValueError):
        s.set("tickets", "stopped")

def test_listener_called_and_errors_swallowed(db):
    s = Switches(connect(db))
    seen = []
    def boom(*a):
        raise RuntimeError("boom")
    s.add_listener(boom)
    s.add_listener(lambda *a: seen.append(a))
    s.set("tickets", "running", scope="p/r", by="dashboard")
    assert seen == [("tickets", "p/r", "running", "dashboard")]
    assert s.get("tickets", "p/r") == "running"

def test_snapshot_shape(db):
    s = Switches(connect(db))
    s.set("tickets", "paused", scope="p/b")
    snap = s.snapshot({"tickets": ["p/a", "p/b"], "reviews": ["p/b"]})
    assert snap["tickets"] == {"global": "paused", "repos": [
        {"scope": "p/a", "state": "running", "effective": "paused"},
        {"scope": "p/b", "state": "paused", "effective": "paused"}]}
    assert snap["reviews"] == {"global": "running", "repos": [
        {"scope": "p/b", "state": "running", "effective": "running"}]}

def test_switch_scopes():
    on = RepoConfig(reviewer=ReviewerConfig(enabled=True))
    projects = {"p": LoadedProject(repos={"b": on, "a": RepoConfig()})}
    assert switch_scopes(projects) == {"tickets": ["p/a", "p/b"], "reviews": ["p/b"]}

def test_default_db_path_is_under_cleavedata(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert reviewer_db_path(ReviewerGlobalConfig()) == tmp_path / "CleaveData" / "reviewer" / "reviewer.db"

def test_connect_creates_dir_and_sets_user_version(db):
    conn = connect(db)
    assert db.parent.is_dir() and (db.parent.stat().st_mode & 0o777) == 0o700
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    connect(db)  # idempotent
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_switches.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.reviewer'`.

- [ ] **Step 3: Implement** `orchestrator/reviewer/db.py` and `orchestrator/switches.py`. `Switches` caches rows in a dict at construction and writes through on `set` (reads stay in memory: `is_running` is called from the poll loop).

- [ ] **Step 4: Run**

Run: `$PY -m pytest tests/unit/test_switches.py -q`
Expected: all pass.

---

### Task 4: Paused tickets stop — `ingest`, `runtime`, `Orchestrator`

**Files:**
- Modify: `orchestrator/ingest.py` (`poll_and_create_workspaces`, `analyze_ticket_ids`: new kwarg `is_repo_running`)
- Modify: `orchestrator/runtime.py` (`Runtime.__init__` kwarg `ticket_gate`; `poll_cycle` step 2)
- Modify: `orchestrator/orchestrator.py` (`_switches`, `set_switches`, `_tickets_running`; both `Runtime(...)` constructions — `__init__` and the lazy one near line 443; both ingest calls)
- Test: `tests/unit/test_ingest.py`, `tests/unit/test_runtime_ticket_gate.py` (new), `tests/unit/test_orchestrator_modes.py`

**Interfaces:**
- Consumes: `Switches.is_running("tickets", project_id, repo_id)` (Task 3)
- Produces:
  - `poll_and_create_workspaces(..., is_repo_running: Callable[[str, str], bool] | None = None)` — when given: a project none of whose repos is running is not polled at all; a routed ticket for a paused repo gets no workspace (checked before the capacity count)
  - `analyze_ticket_ids(..., is_repo_running=None)` — a ticket routed to a paused repo goes to `invalid` as `"<id>: tickets are paused for <project>/<repo> — send «тикеты старт»"`
  - `Runtime(..., ticket_gate: Callable[[str, str], bool] | None = None)` — `poll_cycle` advances only workspaces with `ticket_gate(ws.state.company_id, ws.state.repo_id)` true (a stage already running finishes inside the current cycle's `gather`)
  - `Orchestrator.set_switches(switches) -> None`; `Orchestrator._tickets_running(project_id: str, repo_id: str) -> bool` — `True` when no switches are set (keeps every existing test's behaviour), else `switches.is_running("tickets", …)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_ingest.py (append; reuses _make_loaded_project)
def _two_repo_project():
    from config.schemas import RepoConfig, RepoInfo
    proj = _make_loaded_project("acme")
    proj.repos = {"a": RepoConfig(repo=RepoInfo(id="a"), tracker_label="la"),
                  "b": RepoConfig(repo=RepoInfo(id="b"), tracker_label="lb")}
    return proj

def _ticket(tid, label):
    from integrations.base.tracker import TicketData
    return TicketData(id=tid, url="u", summary="s", description="d", labels=["ai-pipeline", label])

async def _poll(tmp_path, tracker, project, gate, create):
    from config.schemas import GlobalConfig, WorkspacesConfig
    from orchestrator.ingest import poll_and_create_workspaces
    return await poll_and_create_workspaces(
        trackers={"acme": tracker}, projects={"acme": project}, active_workspaces=[],
        global_config=GlobalConfig(workspaces=WorkspacesConfig(base_dir=str(tmp_path))),
        workspace_manager=MagicMock(), default_model_provider=lambda: "m", repo_vcs={},
        notifier=None, dry_run=False, event_bus=None, create_workspace_fn=create,
        is_repo_running=gate)

@pytest.mark.asyncio
async def test_poll_skips_tracker_when_all_repos_paused(tmp_path):
    tracker = AsyncMock()
    await _poll(tmp_path, tracker, _two_repo_project(), lambda p, r: False, AsyncMock())
    tracker.poll_tickets.assert_not_awaited()

@pytest.mark.asyncio
async def test_poll_creates_no_workspace_for_paused_repo(tmp_path):
    tracker = AsyncMock()
    tracker.poll_tickets.return_value = [_ticket("ACME-1", "la"), _ticket("ACME-2", "lb")]
    create = AsyncMock(return_value=MagicMock())
    await _poll(tmp_path, tracker, _two_repo_project(), lambda p, r: r == "b", create)
    assert [c.args[0].ticket.id for c in create.await_args_list] == ["ACME-2"]

@pytest.mark.asyncio
async def test_analyze_refuses_paused_repo():
    from orchestrator.ingest import analyze_ticket_ids
    tracker = AsyncMock()
    tracker.get_ticket.return_value = _ticket("ACME-1", "la")
    create = AsyncMock()
    result = await analyze_ticket_ids(
        ["ACME-1"], trackers={"acme": tracker}, projects={"acme": _two_repo_project()},
        active_workspaces=[], workspace_manager=MagicMock(), default_model_provider=lambda: "m",
        repo_vcs={}, dry_run=False, create_workspace_fn=create, is_repo_running=lambda p, r: False)
    assert result["valid"] == []
    assert "tickets are paused for acme/a" in result["invalid"][0]
    create.assert_not_awaited()
```

```python
# tests/unit/test_runtime_ticket_gate.py
from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from orchestrator.runtime import Runtime
from workspace.workspace import Stage

def _ws(repo):
    return SimpleNamespace(state=SimpleNamespace(company_id="acme", repo_id=repo, current_state=Stage.DEV, ticket_id=repo))

async def test_poll_cycle_does_not_advance_paused_repo():
    advance = AsyncMock()
    wm = MagicMock()
    wm.cleanup_old_workspaces.return_value = []
    rt = Runtime(SimpleNamespace(defaults=SimpleNamespace(max_parallel_tickets=3, max_parallel_builds=1, poll_interval_seconds=60),
                                 workspaces=SimpleNamespace(max_age_days=7)),
                 wm, poll_callback=AsyncMock(), advance_callback=advance,
                 ticket_gate=lambda p, r: r == "b")
    rt.reconcile_disk_workspaces = lambda: None
    rt._active_workspaces = [_ws("a"), _ws("b")]
    await rt.poll_cycle()
    assert [c.args[0].state.repo_id for c in advance.await_args_list] == ["b"]
```

```python
# tests/unit/test_orchestrator_modes.py (append; build the Orchestrator the way this file already does)
def test_tickets_running_without_switches_is_true(orchestrator):
    assert orchestrator._tickets_running("acme", "app") is True

def test_tickets_running_reads_switches(orchestrator, tmp_path):
    from orchestrator.reviewer.db import connect
    from orchestrator.switches import Switches
    orchestrator.set_switches(Switches(connect(tmp_path / "r.db")))
    assert orchestrator._tickets_running("acme", "app") is False  # first boot: paused
```

(If `test_orchestrator_modes.py` has no `orchestrator` fixture, add one from the construction pattern it already uses; ledger it.)

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_ingest.py tests/unit/test_runtime_ticket_gate.py tests/unit/test_orchestrator_modes.py -q`
Expected: FAIL — `unexpected keyword argument 'is_repo_running'` / `'ticket_gate'`, no `_tickets_running`.

- [ ] **Step 3: Implement** as in the Interfaces block. Pass `is_repo_running=self._tickets_running` from both Orchestrator shims and `ticket_gate=lambda p, r: self._tickets_running(p, r)` to both `Runtime(...)` constructions; `_tickets_running` reads `getattr(self, "_switches", None)` so the lazy test-compat construction keeps working.

- [ ] **Step 4: Run the touched files, then the suite**

Run: `$PY -m pytest tests/unit/test_ingest.py tests/unit/test_runtime_ticket_gate.py tests/unit/test_orchestrator_modes.py tests/unit/test_orchestrator_poll_create.py tests/unit/test_runtime_build_semaphore.py -q && $PY -m pytest tests/unit tests/integration -q > $WS/t4.log 2>&1; tail -3 $WS/t4.log`
Expected: all pass; suite = baseline + the new tests, no failures.

---

### Task 5: Telegram fixed commands

**Files:**
- Create: `integrations/telegram/switch_commands.py`
- Modify: `integrations/telegram/command_handler.py` (`__init__` kwargs `switches`, `get_projects`; `handle_message` runs the fixed commands right after the allowlist check; `_handle_status`'s summary branch extracted to `_summary_text(workspaces) -> str`)
- Test: `tests/unit/test_switch_commands.py` (new), `tests/unit/test_command_handler.py` (append)

**Interfaces:**
- Consumes: `Switches`, `switch_scopes`, `GLOBAL` (Task 3)
- Produces:
  - `@dataclass(frozen=True) SwitchCommand(kind: str, mode: str = "", state: str = "", repo: str = "")` — `kind` is `"set"` or `"status"`
  - `parse_switch_command(text: str) -> SwitchCommand | None` — lower-case, trim, drop a trailing `.` or `!`, split on whitespace. One token in `{"статус", "status"}` → status. Two or three tokens: mode word, state word, optional repo token matching `[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?` → set. Anything else → `None` (goes to the LLM as today).
    Mode words: `тикеты`, `тікети`, `tickets` → `tickets`; `ревью`, `рев'ю`, `ревʼю`, `reviews` → `reviews`. State words: `пауза`, `pause` → `paused`; `старт`, `start` → `running`.
  - `resolve_scope(repo: str, scopes: list[str]) -> tuple[str | None, str]` — exact `p/r`, or a unique `r`; errors:
    `Не знаю репо «<repo>». Есть: <a, b | —>` and `«<repo>» есть в нескольких проектах: <p1/x, p2/x> — напиши проект/репо`
  - `format_switch_status(snapshot: dict) -> str` (exact format in the test)
  - A set for `reviews` on a repo that exists only in the tickets scopes answers `В <p/r> ревью не включены (reviewer.enabled в YAML)` and changes nothing.
  - After a set the bot answers with `format_switch_status(...)`; «статус» answers `format_switch_status(...) + "\n\n" + self._summary_text(workspaces)`. Both are sent with `notifier.send_message` (no "Processing…" message) and never call the intent parser. `switches.set(..., by="telegram")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_switch_commands.py
from integrations.telegram.switch_commands import (
    SwitchCommand, format_switch_status, parse_switch_command, resolve_scope)

def test_parse():
    assert parse_switch_command("ревью пауза") == SwitchCommand("set", "reviews", "paused", "")
    assert parse_switch_command("  Тикеты старт acme/app ") == SwitchCommand("set", "tickets", "running", "acme/app")
    assert parse_switch_command("tickets pause app.") == SwitchCommand("set", "tickets", "paused", "app")
    assert parse_switch_command("рев'ю старт") == SwitchCommand("set", "reviews", "running", "")
    assert parse_switch_command("Статус") == SwitchCommand("status")
    for other in ("пости 1 2", "ревью пауза всё сейчас", "ревью", "статус тикетов", "status ACME-1"):
        assert parse_switch_command(other) is None

def test_resolve_scope():
    scopes = ["acme/app", "acme/web", "beta/app"]
    assert resolve_scope("acme/web", scopes) == ("acme/web", "")
    assert resolve_scope("web", scopes) == ("acme/web", "")
    scope, err = resolve_scope("app", scopes)
    assert scope is None and "acme/app, beta/app" in err
    scope, err = resolve_scope("nope", scopes)
    assert scope is None and err.startswith("Не знаю репо «nope»")

def test_format_switch_status():
    snap = {"tickets": {"global": "paused", "repos": [
                {"scope": "acme/app", "state": "running", "effective": "paused"},
                {"scope": "acme/web", "state": "paused", "effective": "paused"}]},
            "reviews": {"global": "running", "repos": [
                {"scope": "acme/app", "state": "running", "effective": "running"}]}}
    assert format_switch_status(snap) == (
        "Тикеты: пауза\n· acme/app — пауза (глобальная)\n· acme/web — пауза\n"
        "Ревью: работает\n· acme/app — работает")
    empty = {"tickets": {"global": "running", "repos": []}, "reviews": {"global": "running", "repos": []}}
    assert format_switch_status(empty) == "Тикеты: работает\n· нет репо\nРевью: работает\n· нет репо с ревью"
```

```python
# tests/unit/test_command_handler.py (append)
from config.schemas import LoadedProject, RepoConfig, ReviewerConfig
from orchestrator.reviewer.db import connect
from orchestrator.switches import Switches

def _switch_handler(tmp_path, parser, notifier, mode_handler):
    projects = {"acme": LoadedProject(repos={"app": RepoConfig(reviewer=ReviewerConfig(enabled=True)),
                                             "web": RepoConfig()})}
    sw = Switches(connect(tmp_path / "r.db"))
    h = CommandHandler(intent_parser=parser, notifier=notifier, mode_handler=mode_handler,
                       active_workspaces_fn=lambda: [], allowed_chat_ids={"1"},
                       switches=sw, get_projects=lambda: projects)
    return h, sw

class TestSwitchCommands:
    async def test_pause_reviews_skips_llm(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, sw = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        await h.handle_message("ревью пауза", "1")
        assert sw.get("reviews") == "paused"
        mock_intent_parser.parse.assert_not_awaited()
        assert "Ревью: пауза" in mock_notifier.send_message.await_args.args[1]

    async def test_repo_scoped_and_not_enabled(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, sw = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        await h.handle_message("тикеты старт web", "1")
        assert sw.get("tickets", "acme/web") == "running"
        await h.handle_message("ревью пауза web", "1")
        assert sw.get("reviews", "acme/web") == "running"
        assert "ревью не включены" in mock_notifier.send_message.await_args.args[1]

    async def test_status_shows_switches_and_summary(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, _ = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        await h.handle_message("статус", "1")
        text = mock_notifier.send_message.await_args.args[1]
        assert text.startswith("Тикеты: пауза") and "Ревью: работает" in text
        assert text.split("\n\n", 1)[1]  # the existing summary follows
        mock_intent_parser.parse.assert_not_awaited()

    async def test_disallowed_chat_ignored(self, tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler):
        h, sw = _switch_handler(tmp_path, mock_intent_parser, mock_notifier, mock_mode_handler)
        await h.handle_message("ревью пауза", "999")
        assert sw.get("reviews") == "running"
        mock_notifier.send_message.assert_not_awaited()
```

(The `mock_*` fixtures live inside `TestCommandHandler`; move them to module level so both classes use them, unchanged.)

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_switch_commands.py tests/unit/test_command_handler.py -q`
Expected: FAIL — `ModuleNotFoundError: integrations.telegram.switch_commands`, `unexpected keyword argument 'switches'`.

- [ ] **Step 3: Implement** as in the Interfaces block.

- [ ] **Step 4: Run**

Run: `$PY -m pytest tests/unit/test_switch_commands.py tests/unit/test_command_handler.py -q`
Expected: all pass, the existing command-handler tests included.

---

### Task 6: Dashboard switches — API, panel, tooltip fix

**Files:**
- Create: `dashboard/switches_api.py`, `dashboard/static/js/switches.js`
- Modify: `dashboard/web.py` (`create_app(..., switches=None)`; extend routes with `build_switch_routes` next to the action routes)
- Modify: `dashboard/static/js/api.js` (`loadSwitches()`, `setSwitch(mode, state, scope)`), `dashboard/static/js/board.js` (both render branches get `<div id="switch-panel-container"></div>` before the health strip and call `renderSwitchPanel(el, 'tickets')`), `dashboard/static/js/app.js:186-200` (tooltip and confirm copy), `dashboard/static/style.css` (`.switch-panel`, `.switch-pill`, `.running`, `.paused`, `.inherited`)
- Test: `tests/unit/test_dashboard_switches.py` (new)

**Interfaces:**
- Consumes: `Switches`, `switch_scopes`, `MODES`, `STATES`, `GLOBAL` (Task 3)
- Produces:
  - `build_switch_routes(switches: Switches, get_projects: Callable[[], dict]) -> list[Route]`
  - `GET /api/switches` → `switches.snapshot(switch_scopes(get_projects()))`
  - `PUT /api/switches` body `{"mode", "state", "scope"}` (`scope` defaults to `"*"`) → 200 with the new snapshot after `switches.set(..., by="dashboard")`; 400 `{"error": "<reason>"}` for an unknown mode or state, or a scope that is neither `"*"` nor in `switch_scopes(...)[mode]`
  - `create_app` registers these routes whenever `switches` is given (with or without an orchestrator) and passes `lambda: projects or {}` (the same dict the Orchestrator mutates on hot-add)
  - `renderSwitchPanel(container, mode)` in `switches.js` — one global pill `Tickets: running ▶` / `Tickets: paused ⏸` (`Reviews: …` for `reviews`) and one pill per repo `<scope> ▶|⏸` showing `effective`; a repo pill whose own state is running but effective is paused gets class `inherited` and title `Paused globally`. Clicking the global pill PUTs the opposite of `global`; clicking a repo pill PUTs the opposite of that repo's own `state`; the panel re-renders from the response. Plan 3 mounts it with `'reviews'` on the Reviews tab.

Tooltip copy (exact): the Manual paragraph becomes `Tickets stop at approval gates (Analysis, QA, PR Review) and wait for your approval in the dashboard. Polling continues — use the Tickets switch on the Board to pause the pipeline.`; the warn for switching to Manual becomes `Switching to <strong>Manual</strong> enables approval gates; polling continues. Tickets will wait for your approval at each stage.`; the warn for switching to Auto becomes `Switching to <strong>Auto</strong> skips approval gates. Pending approvals will be auto-approved.`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_dashboard_switches.py
from __future__ import annotations
import pytest
from starlette.testclient import TestClient
from config.schemas import LoadedProject, RepoConfig, ReviewerConfig
from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from orchestrator.reviewer.db import connect
from orchestrator.switches import Switches

@pytest.fixture
async def client(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    await store.initialize()
    projects = {"acme": LoadedProject(repos={"app": RepoConfig(reviewer=ReviewerConfig(enabled=True)),
                                             "web": RepoConfig()})}
    sw = Switches(connect(tmp_path / "r.db"))
    yield TestClient(create_app(EventBus(), store, projects=projects, switches=sw)), sw
    await store.close()

def test_get_snapshot(client):
    c, _ = client
    body = c.get("/api/switches").json()
    assert body["tickets"]["global"] == "paused"
    assert [r["scope"] for r in body["tickets"]["repos"]] == ["acme/app", "acme/web"]
    assert [r["scope"] for r in body["reviews"]["repos"]] == ["acme/app"]

def test_put_sets_and_returns_snapshot(client):
    c, sw = client
    r = c.put("/api/switches", json={"mode": "tickets", "state": "running"})
    assert r.status_code == 200 and r.json()["tickets"]["global"] == "running"
    r = c.put("/api/switches", json={"mode": "reviews", "state": "paused", "scope": "acme/app"})
    assert r.status_code == 200 and sw.get("reviews", "acme/app") == "paused"

@pytest.mark.parametrize("payload", [
    {"mode": "deploys", "state": "running"},
    {"mode": "tickets", "state": "stopped"},
    {"mode": "tickets", "state": "paused", "scope": "acme/nope"},
    {"mode": "reviews", "state": "paused", "scope": "acme/web"},
])
def test_put_rejects_bad_input(client, payload):
    c, sw = client
    r = c.put("/api/switches", json=payload)
    assert r.status_code == 400 and r.json()["error"]
    assert sw.get("reviews", "acme/web") == "running"
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_dashboard_switches.py -q`
Expected: FAIL — `create_app() got an unexpected keyword argument 'switches'`.

- [ ] **Step 3: Implement** the routes, the JS panel and CSS, and the tooltip copy.

- [ ] **Step 4: Run**

Run: `$PY -m pytest tests/unit/test_dashboard_switches.py tests/unit/test_dashboard_web.py tests/unit/test_dashboard_actions.py -q`
Expected: all pass. The panel itself is checked in a browser in Task 11.

---

### Task 7: Credential checks report HTTP status; Telegram and `claude` login checks

**Files:**
- Modify: `integrations/config/config_tools.py` (`validate_jira`, `validate_github`, `validate_gitlab`, `validate_trello`: every non-200 failure dict gains `"status": <int>`; timeouts and request errors stay without it; Trello's `httpx.HTTPStatusError` returns `e.response.status_code`)
- Modify: `health/validators.py` (`check_jira/github/gitlab/trello` failures set `extra={"status": result.get("status")}`; new `check_telegram`, `check_claude_login`)
- Test: `tests/unit/test_config_tools.py`, `tests/unit/test_health_validators.py` (append)

**Interfaces:**
- Produces:
  - `async check_telegram(token: str) -> ValidatorResult` — `GET https://api.telegram.org/bot<token>/getMe`; 200 with `"ok": true` → ok (`name="telegram"`, `target="bot"`); other status → `ok=False`, `reason=f"Telegram returned HTTP {code}"`, `extra={"status": code}`; any exception → `reason=type(e).__name__`, `extra={"status": None}` (never `str(e)`: it contains the URL)
  - `async check_claude_login(claude_bin: str, timeout_seconds: float = 30.0) -> ValidatorResult` — runs `[claude_bin, "auth", "status", "--json"]` with the scrubbed environment (Global Constraints), parses stdout as JSON whatever the exit code (logged out exits 1 with JSON); `name="claude_login"`, `target="host"`;
    `loggedIn: true` → `ok=True, extra={"logged_in": True, "expires_at": <loginExpiresAt str | None>, "status": None}`;
    `loggedIn: false` → `ok=False, reason="not logged in", extra={"logged_in": False, "expires_at": None, "status": None}`;
    timeout (process killed), missing binary, non-JSON output → `ok=False`, reason `"claude auth status timed out"` / `"claude not found"` / `"unexpected claude auth status output"`, `extra={"logged_in": None, "expires_at": None, "status": None}`

The real CLI on the host (2.1.318, checked 2026-11-12): logged in → exit 0, `{"loggedIn": true, …, "loginExpiresAt": "2026-11-25T…Z", …}`; keychain locked → exit 1, `{"loggedIn": false, "authMethod": "none", …, "loginExpiresAt": null}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_config_tools.py (append; respx as the file already uses it)
@respx.mock
async def test_validate_github_failure_carries_status():
    respx.get("https://api.github.com/repos/o/r").respond(401)
    assert (await config_tools.validate_github(token="t", owner="o", repo="r"))["status"] == 401

@respx.mock
async def test_validate_jira_failure_carries_status():
    respx.get("https://x/rest/api/3/project/K").respond(404)
    assert (await config_tools.validate_jira(url="https://x", token="t", email="e", project_key="K"))["status"] == 404

@respx.mock
async def test_validate_trello_auth_failure_carries_status():
    respx.route(url__startswith="https://api.trello.com/1/members/me").respond(401)
    assert (await config_tools.validate_trello(api_key="k", token="t", board_id="b"))["status"] == 401

@respx.mock
async def test_validate_github_timeout_has_no_status():
    respx.get("https://api.github.com/repos/o/r").mock(side_effect=httpx.ConnectTimeout("x"))
    assert "status" not in await config_tools.validate_github(token="t", owner="o", repo="r")
```

```python
# tests/unit/test_health_validators.py (append)
import json, os, stat, time
import httpx, respx
from health.validators import check_claude_login, check_github, check_telegram

def _fake_claude(tmp_path, body: str) -> str:
    p = tmp_path / "claude"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)

async def test_check_github_failure_passes_status():
    with patch("health.validators.config_tools.validate_github",
               new=AsyncMock(return_value={"success": False, "error": "HTTP 403", "status": 403})):
        assert (await check_github("t", "o", "r")).extra["status"] == 403

@respx.mock
async def test_check_telegram_ok_and_401():
    route = respx.get("https://api.telegram.org/bot123:abc/getMe")
    route.respond(200, json={"ok": True, "result": {}})
    assert (await check_telegram("123:abc")).ok
    route.respond(401, json={"ok": False})
    r = await check_telegram("123:abc")
    assert not r.ok and r.extra["status"] == 401

@respx.mock
async def test_check_telegram_network_error_hides_token():
    respx.get("https://api.telegram.org/bot123:SECRET/getMe").mock(side_effect=httpx.ConnectError("https://api.telegram.org/bot123:SECRET/getMe"))
    r = await check_telegram("123:SECRET")
    assert r.reason == "ConnectError" and "SECRET" not in repr(r)

async def test_check_claude_login_logged_in(tmp_path):
    bin_ = _fake_claude(tmp_path, '[ "$*" = "auth status --json" ] || exit 9\n'
                                  'echo \'{"loggedIn": true, "loginExpiresAt": "2026-11-25T10:00:00.000Z"}\'\n')
    r = await check_claude_login(bin_)
    assert r.ok and r.extra["expires_at"] == "2026-11-25T10:00:00.000Z"

async def test_check_claude_login_logged_out_exit_1(tmp_path):
    r = await check_claude_login(_fake_claude(tmp_path, 'echo \'{"loggedIn": false, "loginExpiresAt": null}\'\nexit 1\n'))
    assert not r.ok and r.extra["logged_in"] is False

async def test_check_claude_login_env_is_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_API_KEY", "k1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k2")
    monkeypatch.setenv("SOME_OTHER_VAR", "x")
    dump = tmp_path / "env.txt"
    bin_ = _fake_claude(tmp_path, f'env > {dump}\necho \'{{"loggedIn": true, "loginExpiresAt": null}}\'\n')
    assert (await check_claude_login(bin_)).ok
    keys = {line.split("=", 1)[0] for line in dump.read_text().splitlines()}
    assert {"HOME", "PATH"} <= keys          # the shell itself may add PWD, SHLVL, _
    assert not keys & {"CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "SOME_OTHER_VAR"}

async def test_check_claude_login_timeout_kills_and_reports(tmp_path):
    t0 = time.monotonic()
    r = await check_claude_login(_fake_claude(tmp_path, "sleep 30\n"), timeout_seconds=0.5)
    assert not r.ok and "timed out" in r.reason and time.monotonic() - t0 < 5

async def test_check_claude_login_non_json(tmp_path):
    r = await check_claude_login(_fake_claude(tmp_path, "echo 'Not logged in · Please run /login'\n"))
    assert not r.ok and r.extra["logged_in"] is None

async def test_check_claude_login_missing_binary(tmp_path):
    r = await check_claude_login(str(tmp_path / "nope"))
    assert not r.ok and r.reason == "claude not found"
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_config_tools.py tests/unit/test_health_validators.py -q`
Expected: FAIL — `KeyError: 'status'`, `ImportError: cannot import name 'check_claude_login'`.

- [ ] **Step 3: Implement** as in the Interfaces block.

- [ ] **Step 4: Run**

Run: `$PY -m pytest tests/unit/test_config_tools.py tests/unit/test_health_validators.py tests/unit/test_health_runner.py -q`
Expected: all pass.

---

### Task 8: `CredentialHealth` service

**Files:**
- Create: `health/credentials.py`
- Test: `tests/unit/test_credential_health.py`

**Interfaces:**
- Consumes: `check_jira`, `check_trello`, `check_github`, `check_gitlab`, `check_telegram`, `check_claude_login` (Task 7); `env_refs` (Task 1)
- Produces:
  - `@dataclass Credential(key, kind, provider, label, target, env_var, field, check: Callable[[], Awaitable[ValidatorResult]])` — keys `forge:<project>/<repo>`, `tracker:<project>`, `telegram`, `claude_login`; kinds `forge`, `tracker`, `telegram`, `claude_login`; providers `github`, `gitlab`, `jira`, `trello`, `telegram`, `claude`; labels `GitHub <owner>/<repo>`, `GitLab <project_id>`, `Jira <project_key>`, `Trello <board_id>`, `Telegram bot`, `claude login`; `field` is the YAML path (`vcs.github.token`, `tracker.jira.token`, `tracker.trello.token`, `telegram.bot_token`); `env_var` from the matching `env_refs` (legacy top-level `jira.token` also accepted), `""` when the YAML holds the value inline
  - `build_credentials(global_config: GlobalConfig, projects: dict[str, LoadedProject], claude_bin: str) -> list[Credential]` — in this order: per project (dict order) its tracker (Jira when `url` and `token` are set, Trello when `api_key` and `token` are set), then its repos' forges (dict order, only with a token); then `telegram` for the token the daemon uses (global, else the first project that has one — the `main.py:369-381` rule), if any; always `claude_login` last
  - `@dataclass CredentialStatus(key, kind, provider, label, target, env_var, field, state="unknown", reason="", fix="", http_status: int | None = None, since: datetime | None = None, checked_at: datetime | None = None, expires_at: datetime | None = None)` with `to_dict() -> dict` (datetimes as ISO strings or `None`)
  - `classify(cred: Credential, result: ValidatorResult, now: datetime) -> tuple[str, datetime | None]` — state and expiry:
    - `claude_login`: `logged_in` None → `unreachable`; False → `invalid`; True with `expires_at` ≤ now → `invalid`; ≤ now + 3 days → `expiring`; otherwise `ok`
    - `telegram`: ok → `ok`; status 401 or 404 → `invalid`; else as below
    - others: ok → `ok`; 401 → `invalid`; 403, 404 → `no_access`; `None`, 429, ≥ 500, anything else → `unreachable`
  - `fix_text(cred: Credential, state: str) -> str` — exact copy:
    - `invalid` (forge, tracker, telegram) with `env_var`: `Створи новий токен, поклади в .env як {env_var}, перезапусти демон.`; without: `Створи новий токен, впиши його в {field}, перезапусти демон.`
    - `no_access`, GitHub: `Перевір доступ токена до {target}; classic-токену GitHub може знадобитися SSO-авторизація для організації.`; other providers: `Перевір доступ токена до {target}.`
    - `invalid` or `expiring`, `claude_login`: `Перелогінься на хості в GUI-терміналі.`
    - `unreachable`: `Мережа або сервіс недоступні; наступна перевірка за 15 хв.`
    - `ok`, `unknown`: `""`
  - `class CredentialHealth(credentials_fn: Callable[[], list[Credential]], send_line: Callable[[str], Awaitable[Any]] | None = None, *, now=lambda: datetime.now(timezone.utc), interval_seconds: float = 900.0, recheck_gap_seconds: float = 60.0, tz: tzinfo | None = None)`:
    - `async check_all() -> list[CredentialStatus]` — calls `credentials_fn()` each time (hot-added projects appear), runs every check concurrently, applies results
    - `async check_one(key: str) -> CredentialStatus | None`
    - `report_auth_failure(key: str, status: int) -> None` — synchronous, never raises; schedules `check_one(key)` on the running loop unless that key was checked less than `recheck_gap_seconds` ago or a check of it is in flight. The state always comes from the re-check, never from the report.
    - `snapshot() -> list[CredentialStatus]` — in `credentials_fn()` order; never-checked ones as `unknown`
    - `state(key) -> str` (`"unknown"` for never-checked or unconfigured); `failing(key) -> bool` — state in `{"invalid", "no_access"}`
    - `async run(stop: asyncio.Event) -> None` — `check_all()`, then wait `interval_seconds` or `stop`; an exception in a round is logged and the loop continues
  - `since` changes whenever `state` changes. Telegram lines (never for `kind == "telegram"`), with "previous" meaning the last state that was not `unknown`/`unreachable` (none yet counts as `ok`):
    - ok/expiring → invalid: `⚠️ {label}: недійсний або прострочений. {fix}`
    - ok/expiring → no_access: `⚠️ {label}: немає доступу (HTTP {http_status}). {fix}`
    - invalid/no_access → ok/expiring: `✅ {label}: знову працює.`
    - `claude_login` with `expires_at`: when ≤ 1 day left and the 1-day line for this `expires_at` was not sent → `⏳ Вхід claude на хості спливає {expires_at in tz, %Y-%m-%d %H:%M}. Перелогінься на хості в GUI-терміналі.` (marks both 1-day and 3-day as sent); else when ≤ 3 days left and the 3-day line was not sent → the same line. A new `expires_at` (re-login) starts fresh.
    - A failing `send_line` is logged; it never fails the check.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_credential_health.py
from __future__ import annotations
import asyncio, json
from datetime import datetime, timedelta, timezone
import pytest
from config.schemas import (GitHubConfig, GlobalConfig, LoadedProject, ProjectConfig, RepoConfig,
                            TelegramConfig, VCSConfig)
from health.credentials import Credential, CredentialHealth, build_credentials, classify
from health.validators import ValidatorResult

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

def ok():
    return ValidatorResult(True, "x", "t", "", "")

def http(code):
    return ValidatorResult(False, "x", "t", f"HTTP {code}", "", extra={"status": code})

def login(logged_in, expires=None):
    return ValidatorResult(bool(logged_in), "claude_login", "host", "", "",
                           extra={"logged_in": logged_in, "expires_at": expires, "status": None})

class Scripted:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0
    async def __call__(self):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]

def cred(key="forge:acme/app", kind="forge", provider="github", check=None, env_var="GH_T"):
    return Credential(key, kind, provider, "GitHub acme-org/acme-app", "acme-org/acme-app",
                      env_var, "vcs.github.token", check or Scripted(ok()))

def service(creds, lines, now=lambda: NOW, **kw):
    async def send(text):
        lines.append(text)
    return CredentialHealth(lambda: creds, send, now=now, tz=timezone.utc, **kw)

@pytest.mark.parametrize("kind,result,state", [
    ("forge", ok(), "ok"), ("forge", http(401), "invalid"), ("forge", http(403), "no_access"),
    ("forge", http(404), "no_access"), ("forge", http(502), "unreachable"), ("forge", http(429), "unreachable"),
    ("tracker", ValidatorResult(False, "jira", "K", "timed out", "", extra={"status": None}), "unreachable"),
    ("telegram", http(404), "invalid"), ("telegram", http(401), "invalid"),
    ("claude_login", login(True, "2026-12-30T00:00:00Z"), "ok"),
    ("claude_login", login(True, "2026-11-22T12:00:00Z"), "expiring"),
    ("claude_login", login(True, "2026-11-20T11:00:00Z"), "invalid"),
    ("claude_login", login(False), "invalid"), ("claude_login", login(None), "unreachable"),
])
def test_classify(kind, result, state):
    assert classify(cred(kind=kind), result, NOW)[0] == state

async def test_failing_sends_one_line_then_recovery():
    lines = []
    c = cred(check=Scripted(http(401), http(401), ok()))
    h = service([c], lines)
    await h.check_all(); await h.check_all()
    assert h.failing("forge:acme/app")
    assert lines == ["⚠️ GitHub acme-org/acme-app: недійсний або прострочений. "
                     "Створи новий токен, поклади в .env як GH_T, перезапусти демон."]
    await h.check_all()
    assert lines[-1] == "✅ GitHub acme-org/acme-app: знову працює." and len(lines) == 2

async def test_no_access_line_has_sso_hint():
    lines = []
    await service([cred(check=Scripted(http(403)))], lines).check_all()
    assert lines == ["⚠️ GitHub acme-org/acme-app: немає доступу (HTTP 403). Перевір доступ токена до "
                     "acme-org/acme-app; classic-токену GitHub може знадобитися SSO-авторизація для організації."]

async def test_unreachable_is_silent_and_keeps_failing_memory():
    lines = []
    h = service([cred(check=Scripted(http(401), http(503), http(401), http(503), ok()))], lines)
    for _ in range(5):
        await h.check_all()
    assert len(lines) == 2 and lines[1].startswith("✅")

async def test_telegram_credential_never_sends():
    lines = []
    await service([cred("telegram", "telegram", "telegram", Scripted(http(401)))], lines).check_all()
    assert lines == []

async def test_login_expiry_warnings_once_each():
    lines, clock = [], [NOW]
    c = cred("claude_login", "claude_login", "claude", Scripted(login(True, "2026-11-24T12:00:00Z")), env_var="")
    h = service([c], lines, now=lambda: clock[0])
    await h.check_all()                                   # 4 days left: nothing
    clock[0] = NOW + timedelta(days=1, hours=1); await h.check_all()   # < 3 days
    clock[0] = NOW + timedelta(days=1, hours=2); await h.check_all()   # still < 3 days: nothing new
    clock[0] = NOW + timedelta(days=3, hours=1); await h.check_all()   # < 1 day
    assert lines == ["⏳ Вхід claude на хості спливає 2026-11-24 12:00. Перелогінься на хості в GUI-терміналі."] * 2
    assert h.state("claude_login") == "expiring"

async def test_login_first_seen_under_one_day_sends_one_line():
    lines = []
    c = cred("claude_login", "claude_login", "claude", Scripted(login(True, "2026-11-21T00:00:00Z")), env_var="")
    await service([c], lines).check_all()
    assert len(lines) == 1

async def test_report_auth_failure_rechecks_once_within_gap():
    check = Scripted(ok(), http(401))
    h = service([cred(check=check)], [], recheck_gap_seconds=60)
    h.report_auth_failure("forge:acme/app", 401)
    for _ in range(4):
        h.report_auth_failure("forge:acme/app", 401)
    await asyncio.sleep(0.05)
    assert check.calls == 1
    h.report_auth_failure("forge:acme/app", 401)        # checked < 60 s ago
    await asyncio.sleep(0.05)
    assert check.calls == 1

async def test_report_unknown_key_is_harmless():
    service([cred()], []).report_auth_failure("forge:nope/x", 401)
    await asyncio.sleep(0)

async def test_send_failure_does_not_raise():
    async def boom(_):
        raise RuntimeError("telegram down")
    h = CredentialHealth(lambda: [cred(check=Scripted(http(401)))], boom, now=lambda: NOW)
    assert (await h.check_all())[0].state == "invalid"

async def test_credentials_fn_called_per_check():
    creds = [cred()]
    h = service(creds, [])
    await h.check_all()
    creds.append(cred("tracker:beta", "tracker", "jira"))
    await h.check_all()
    assert [s.key for s in h.snapshot()] == ["forge:acme/app", "tracker:beta"]

async def test_unknown_before_first_check_is_not_failing():
    h = service([cred()], [])
    assert h.state("forge:acme/app") == "unknown" and not h.failing("forge:acme/app")

async def test_run_stops_on_event():
    check = Scripted(ok())
    h = service([cred(check=check)], [], interval_seconds=3600)
    stop = asyncio.Event()
    task = asyncio.create_task(h.run(stop))
    await asyncio.sleep(0.05); stop.set()
    await asyncio.wait_for(task, 1)
    assert check.calls == 1

def test_build_credentials_and_no_token_values():
    repo = RepoConfig(vcs=VCSConfig(provider="github", github=GitHubConfig(token="ghp_SECRET", owner="acme-org", repo="acme-app")),
                      env_refs={"vcs.github.token": "GH_T"})
    bare = RepoConfig(vcs=VCSConfig(provider="github", github=GitHubConfig(token="", owner="o", repo="r")))
    proj = LoadedProject(config=ProjectConfig(telegram=TelegramConfig(bot_token="123:TGSECRET"),
                                              env_refs={"telegram.bot_token": "ACME_TG"}),
                         repos={"app": repo, "bare": bare})
    creds = build_credentials(GlobalConfig(), {"acme": proj}, "/fake/claude")
    assert [c.key for c in creds] == ["forge:acme/app", "telegram", "claude_login"]
    assert (creds[0].label, creds[0].env_var) == ("GitHub acme-org/acme-app", "GH_T")
    assert creds[1].env_var == "ACME_TG"

async def test_snapshot_never_contains_token_values():
    repo = RepoConfig(vcs=VCSConfig(provider="github", github=GitHubConfig(token="ghp_SECRET", owner="o", repo="r")),
                      env_refs={"vcs.github.token": "GH_T"})
    creds = build_credentials(GlobalConfig(), {"acme": LoadedProject(repos={"r": repo})}, "/fake/claude")
    for c in creds:
        c.check = Scripted(http(401))
    h = service(creds, [])
    await h.check_all()
    assert "ghp_SECRET" not in json.dumps([s.to_dict() for s in h.snapshot()])
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_credential_health.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'health.credentials'`.

- [ ] **Step 3: Implement** `health/credentials.py` as in the Interfaces block.

- [ ] **Step 4: Run**

Run: `$PY -m pytest tests/unit/test_credential_health.py -q`
Expected: all pass.

---

### Task 9: Adapters report 401/403

**Files:**
- Modify: `integrations/github/github_adapter.py`, `integrations/gitlab/gitlab_adapter.py`, `integrations/jira/jira_adapter.py`, `integrations/trello/trello_adapter.py` (constructor kwarg `on_auth_failure`; call it in `_request`'s existing 401/403 branch, before the raise)
- Test: `tests/unit/test_adapter_auth_report.py` (new)

**Interfaces:**
- Produces: each adapter's `__init__(..., on_auth_failure: Callable[[int], None] | None = None)` (last parameter; keyword use only). `_request` calls `on_auth_failure(status)` inside `try/except Exception` (a failing callback is logged and the request still raises the same `HTTPStatusError` as today). Other request paths (GraphQL, raw downloads) are unchanged; the 15-minute check covers them.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_adapter_auth_report.py
from __future__ import annotations
import httpx, pytest, respx
from integrations.github.github_adapter import GitHubAdapter
from integrations.gitlab.gitlab_adapter import GitLabAdapter
from integrations.jira.jira_adapter import JiraAdapter
from integrations.trello.trello_adapter import TrelloAdapter

def _adapters(cb):
    return [
        (GitHubAdapter("t", "o", "r", on_auth_failure=cb), "https://api.github.com/repos/o/r", "/repos/o/r"),
        (GitLabAdapter("t", "42", on_auth_failure=cb), "https://gitlab.com/api/v4/projects/42", "/projects/42"),
        (JiraAdapter("https://x", "e", "t", "K", on_auth_failure=cb), "https://x/rest/api/3/myself", "/rest/api/3/myself"),
        (TrelloAdapter(api_key="k", token="t", board_id="b", on_auth_failure=cb), "https://api.trello.com/1/members/me", "/members/me"),
    ]

@pytest.mark.parametrize("i", range(4))
@pytest.mark.parametrize("code", [401, 403])
@respx.mock
async def test_auth_failure_is_reported_and_still_raises(i, code):
    seen = []
    adapter, url, path = _adapters(seen.append)[i]
    respx.route(url__startswith=url).respond(code)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("GET", path)
    assert seen == [code]

@pytest.mark.parametrize("i", range(4))
@respx.mock
async def test_success_is_not_reported(i):
    seen = []
    adapter, url, path = _adapters(seen.append)[i]
    respx.route(url__startswith=url).respond(200, json={})
    await adapter._request("GET", path)
    assert seen == []

@respx.mock
async def test_failing_callback_does_not_change_the_error():
    def boom(_):
        raise RuntimeError("x")
    adapter = GitHubAdapter("t", "o", "r", on_auth_failure=boom)
    respx.route(url__startswith="https://api.github.com/repos/o/r").respond(401)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("GET", "/repos/o/r")
```

Each adapter builds its URL from its own `base_url` and the `path` it receives; if a path above does not reach the mocked URL for one adapter, adjust that row's `path` to the adapter's convention (read its `_request`) and ledger it.

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_adapter_auth_report.py -q`
Expected: FAIL — `unexpected keyword argument 'on_auth_failure'`.

- [ ] **Step 3: Implement** the kwarg and the call in the four adapters.

- [ ] **Step 4: Run**

Run: `$PY -m pytest tests/unit/test_adapter_auth_report.py tests/unit/test_github_adapter_request.py tests/unit/test_gitlab_adapter_request.py tests/unit/test_trello_adapter.py tests/integration/test_jira_adapter.py tests/integration/test_github_adapter.py -q`
Expected: all pass.

---

### Task 10: Dashboard credential strip

**Files:**
- Modify: `dashboard/web.py` (`create_app(..., credential_health=None)`; new route; remove `projects_health`, its route `/api/projects/health` and the `from health.runner import check_all` import)
- Modify: `dashboard/static/js/api.js` (`loadHealth(force)` fetches `/api/health/credentials`), `dashboard/static/js/board.js` (`renderHealthStrip` renders credential chips), `dashboard/static/style.css` (`.health-dot.grey`, `.health-strip.grey`)
- Modify: `tests/unit/test_dashboard_health_api.py` (rewrite for the new endpoint), `tests/e2e/test_dashboard_health.py` (rewrite against a fake credential service; not run here)
- Keep: `health/runner.py` and `python -m health.runner` (CLI) unchanged

**Interfaces:**
- Consumes: `CredentialHealth.snapshot()`, `.check_all()`, `CredentialStatus.to_dict()` (Task 8)
- Produces:
  - `GET /api/health/credentials` → `{"credentials": [status.to_dict(), …], "checked_at": <latest checked_at ISO | null>}`; `?refresh=1` awaits `check_all()` first; without a service → `{"credentials": [], "checked_at": null}`
  - Strip: hidden when every credential is `ok` or `unknown` (as today when all green). Otherwise header `<n> credential(s) need attention` with the existing minimize and refresh buttons, and one chip per credential needing attention: dot (`invalid`, `no_access` → red; `expiring` → yellow; `unreachable` → grey), label, state text (`недійсний або прострочений`, `немає доступу`, `спливає <expires_at, local>`, `недоступний`), `<code>{env_var}</code>` when set, `з <since, local>`, `перевірено <checked_at, local>`, `reason`, and `fix` in `<code class="health-check-fix">`. Strip colour: red if any red, else yellow if any yellow, else grey.

- [ ] **Step 1: Write the failing tests** (replace the file's endpoint tests)

```python
# tests/unit/test_dashboard_health_api.py
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from starlette.testclient import TestClient
from dashboard.event_store import EventStore
from dashboard.events import EventBus
from dashboard.web import create_app
from health.credentials import CredentialStatus

T = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

@pytest.fixture
async def store(tmp_path):
    s = EventStore(str(tmp_path / "test.db"))
    await s.initialize()
    yield s
    await s.close()

class FakeHealth:
    def __init__(self):
        self.check_all = AsyncMock()
    def snapshot(self):
        return [CredentialStatus("forge:acme/app", "forge", "github", "GitHub acme-org/acme-app", "acme-org/acme-app",
                                 "GH_T", "vcs.github.token", state="invalid", http_status=401, since=T, checked_at=T)]

def test_credentials_endpoint(store):
    fake = FakeHealth()
    c = TestClient(create_app(EventBus(), store, credential_health=fake))
    body = c.get("/api/health/credentials").json()
    assert body["credentials"][0]["state"] == "invalid" and body["credentials"][0]["env_var"] == "GH_T"
    assert body["checked_at"] == T.isoformat()
    fake.check_all.assert_not_awaited()
    c.get("/api/health/credentials?refresh=1")
    fake.check_all.assert_awaited_once()

def test_credentials_endpoint_without_service(store):
    body = TestClient(create_app(EventBus(), store)).get("/api/health/credentials").json()
    assert body == {"credentials": [], "checked_at": None}

def test_old_projects_health_route_is_gone(store):
    assert TestClient(create_app(EventBus(), store)).get("/api/projects/health").status_code == 404
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m pytest tests/unit/test_dashboard_health_api.py -q`
Expected: FAIL — `unexpected keyword argument 'credential_health'`.

- [ ] **Step 3: Implement** the route, the removals, the JS strip and CSS, and rewrite the e2e test against `credential_health=` (one `invalid` credential; assert the chip shows `недійсний або прострочений` and `GH_T`).

- [ ] **Step 4: Run**

Run: `$PY -m pytest tests/unit/test_dashboard_health_api.py tests/unit/test_dashboard_web.py -q && grep -rn "projects/health\|dashboard.web.check_all" dashboard tests main.py`
Expected: tests pass; grep prints nothing.

---

### Task 11: Wire it into the daemon; smoke run

**Files:**
- Modify: `main.py`
- Test: the suite, plus a smoke run (below)

**Interfaces:**
- Consumes: everything above.
- Changes in `main.py`:
  - `_build_tracker_for_project(cfg, project_id, on_auth_failure=None)` and `_build_vcs_adapter(repo_cfg, on_auth_failure=None)` pass the callback to the adapters; callers pass `lambda status, _k=f"tracker:{project_id}": credential_health.report_auth_failure(_k, status)` and `…f"forge:{project_id}/{repo_id}"…` — at start and in the hot-reload path (`_build_repo_adapters` gains a `project_id` parameter).
  - `credential_health = CredentialHealth(lambda: build_credentials(global_config, projects, claude_bin), send_line=_credential_line)`, built before the adapters; `claude_bin = shutil.which("claude") or "claude"`; `_credential_line(text)` sends to the operator chat (`global_config.telegram.default_chat_id`, else the first project's `telegram.default_chat_id`) when the notifier exists, and does nothing otherwise.
  - `switches = Switches(connect(reviewer_db_path(global_config.reviewer)))`; `orchestrator.set_switches(switches)`; one listener emits `switch_changed` on the event bus (`data={"mode", "scope", "state", "by"}`) and, when tickets go `running`, sets `orchestrator._wake_event`. Print `  Switches: tickets=<global>, reviews=<global>`.
  - `CommandHandler(..., switches=switches, get_projects=lambda: projects)`; `create_app(..., switches=switches, credential_health=credential_health)`.
  - `_run_all`: remove the "Warm the health-check cache" block; start `credential_health.run(stop)` as a task right after the event store is initialised (independent of the dashboard); on shutdown set `stop` and await the task (cancel after 5 s).

- [ ] **Step 1: Suite**

Run: `$PY -m pytest tests/unit tests/integration -q > $WS/t11.log 2>&1; tail -3 $WS/t11.log`
Expected: no failures; count = baseline + this plan's new tests.

- [ ] **Step 2: Smoke run with a fake `claude`**

Write to `$SMOKE` (a scratch directory): `bin/claude` (executable, prints `{"loggedIn": false, "loginExpiresAt": null}` and exits 1), and `cfg/global.yaml`:

```yaml
os: mac
workspaces: {base_dir: "SMOKE/ws"}
logging: {dir: "SMOKE/logs"}
dashboard: {host: 127.0.0.1, port: 18765, db_path: "SMOKE/events.db"}
defaults: {cap_build_memory: false}   # otherwise startup rewrites ~/.gradle/gradle.properties
reviewer: {db_path: "SMOKE/rv/reviewer.db"}
```

(with `SMOKE` replaced by the absolute path). Start: `env -i HOME="$HOME" PATH="$SMOKE/bin:/usr/bin:/bin" $PY main.py --config "$SMOKE/cfg" > "$SMOKE/run.log" 2>&1 &`, wait until port 18765 answers (≤ 30 s), then:

Run: `curl -s 127.0.0.1:18765/api/switches; echo; curl -s '127.0.0.1:18765/api/health/credentials?refresh=1'; echo; curl -s -X PUT -H 'Content-Type: application/json' -d '{"mode":"tickets","state":"running"}' 127.0.0.1:18765/api/switches`
Expected: tickets `paused`, reviews `running`; one credential `claude_login` in state `invalid` with fix `Перелогінься на хості в GUI-терміналі.`; the PUT answers tickets `running`. `run.log` shows `Switches: tickets=paused, reviews=running` and no traceback.

- [ ] **Step 3: Restart keeps the switch; the browser shows the panel and the chip**

Stop the daemon (SIGTERM; it must exit within 10 s), start it again the same way.
Run: `curl -s 127.0.0.1:18765/api/switches`
Expected: tickets `running`. Then open `http://127.0.0.1:18765/` in a browser (chrome-devtools tools are fine): the Board shows `Tickets: running ▶`, clicking it shows `Tickets: paused ⏸` and survives a reload; the health strip shows the `claude login` chip in red with `недійсний або прострочений`. Stop the daemon; delete `$SMOKE`.

- [ ] **Step 4: Leak check**

Run: `grep -rn -i -E -e "$(grep -v -E '^(#|$)' ~/.config/cleave/denylist.txt)" -- config config-live.example dashboard docs/superpowers health integrations orchestrator tests main.py; echo "exit=$?"`
Expected: `exit=1` (no matches). The private denylist never goes into the repo or into this plan; the local `pre-commit` and `commit-msg` hooks run it again at commit time.

- [ ] **Step 5: Hand back to the operator** with the deployment notes below; commit only if asked.

## Deployment notes for plan 6

- The host's `config-live/global.yaml` sections `pipeline`, `intent_parser` and `dashboard` take effect for the first time. Before the first restart, the operator checks them against `PipelineConfig`, `IntentParserConfig` and `DashboardConfig` (an unknown field now stops the daemon; a `dashboard.db_path` there moves the event store; `dashboard.host` there replaces `0.0.0.0`).
- First boot creates `~/CleaveData/reviewer/reviewer.db` with **tickets paused**: the ticket pipeline does nothing until «тикеты старт» (or the Board switch).
- With the current tokens (401 since 2026-11-11) the dashboard shows the forge and tracker chips as `недійсний або прострочений`, and Telegram gets one line each — this is the operator's planned check. After the new token is in `.env` and the daemon restarted, the next check (≤ 15 min, or ↻ on the strip) clears them and sends `✅ … знову працює.`
- The `claude` login on the host expires 2026-11-25: the 3-day line comes on 2026-11-22.
