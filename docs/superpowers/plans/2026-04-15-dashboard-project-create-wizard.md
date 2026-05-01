# Dashboard Project Create Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dashboard wizard for creating new Cleave projects, which persists raw secrets to `.env`, spawns the Atlas agent to write config YAMLs and bootstrap the workspace tree, and streams live status back to the UI.

**Architecture:** A Starlette endpoint (`POST /api/projects/create`) receives a full form payload, persists secrets to `.env` + `os.environ`, creates a setup workspace matching the existing ticket-workspace shape, and spawns Atlas as a supervised asyncio task. A vanilla-JS wizard in the dashboard collects the data and polls the existing `/api/workspaces` endpoint for progress. The existing singular `trigger_label` config field is migrated to a plural `trigger_labels` list with AND semantics as prerequisite Phase 1.

**Tech Stack:** Python 3.12, Starlette, asyncio, PyYAML, pytest; vanilla HTML/CSS/JS (no frameworks added).

**Spec:** [docs/superpowers/specs/2026-04-15-dashboard-project-create-wizard-design.md](../specs/2026-04-15-dashboard-project-create-wizard-design.md)

---

## File Structure

**Backend — new files**
- `dashboard/env_writer.py` — atomic `.env` append, read, collision detection, `0600` chmod
- `dashboard/setup_workspace.py` — creates `{base_dir}/{project_id}/{repo_id}/` tree and `setup/state.json`
- `dashboard/project_create.py` — `POST /api/projects/create` handler, orchestrates validation → secrets → workspace → Atlas spawn → rollback
- `dashboard/atlas_runner.py` — spawns Atlas as a background asyncio task, supervises, writes `SETUP_FAILED` on exception

**Backend — modified files**
- `dashboard/web.py` — register new route; tag setup entries with `kind: "setup"`
- `config/schemas.py` — `trigger_label: str` → `trigger_labels: list[str]`
- `config/config_loader.py` — raise `ConfigError` on legacy `trigger_label` key
- `integrations/jira/jira_adapter.py` — filter on `trigger_labels` with AND semantics (JQL `labels = "x" AND labels = "y"`)
- `orchestrator/ticket_prioritizer.py` — `filter_tickets` accepts `trigger_labels: list[str]`, requires all
- `agents/project-setup-agent.md` — add orchestrator-mode flow that reads `meta/input.md`, bootstraps workspace tree, writes report

**Config / fixtures updated for `trigger_labels` rename**
- `config-live/projects/acme/project.yaml`
- `config-live.example/projects/example-project/project.yaml`
- `tests/fixtures/config/projects/test-project/project.yaml`

**Frontend — new files**
- `dashboard/static/js/project-wizard.js`

**Frontend — modified files**
- `dashboard/static/index.html`
- `dashboard/static/js/app.js`
- `dashboard/static/js/api.js`
- `dashboard/static/style.css`

**Tests — new files**
- `tests/unit/test_env_writer.py`
- `tests/unit/test_project_create_payload.py`
- `tests/unit/test_setup_workspace.py`
- `tests/unit/test_trigger_labels_migration.py`
- `tests/integration/test_project_create_flow.py`

**Tests — modified files**
- `tests/unit/test_config_cascade.py`
- `tests/unit/test_ticket_prioritizer.py`
- `tests/integration/test_jira_adapter.py`

---

# Phase 1 — `trigger_labels` migration

## Task 1: Rename schema field

**Files:**
- Modify: `config/schemas.py:115`
- Test: `tests/unit/test_trigger_labels_migration.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_trigger_labels_migration.py`:

```python
import pytest

from config.schemas import JiraConfig


def test_jira_config_has_trigger_labels_list():
    cfg = JiraConfig()
    assert cfg.trigger_labels == ["ai-pipeline"]
    assert not hasattr(cfg, "trigger_label")


def test_jira_config_accepts_multiple_labels():
    cfg = JiraConfig(trigger_labels=["ai-pipeline", "acme-mobile"])
    assert cfg.trigger_labels == ["ai-pipeline", "acme-mobile"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_trigger_labels_migration.py -v`
Expected: FAIL — `AssertionError: assert 'ai-pipeline' == ['ai-pipeline']`

- [ ] **Step 3: Update schema**

Edit `config/schemas.py` line 115:

```python
@dataclass
class JiraConfig:
    url: str = ""
    token: str = ""
    email: str = ""
    project_key: str = ""
    trigger_labels: list[str] = field(default_factory=lambda: ["ai-pipeline"])
    ignore_labels: list[str] = field(default_factory=list)
    statuses: JiraStatusesConfig = field(default_factory=JiraStatusesConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_trigger_labels_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config/schemas.py tests/unit/test_trigger_labels_migration.py
git commit -m "refactor(config): rename JiraConfig.trigger_label to plural trigger_labels"
```

---

## Task 2: Raise migration error on legacy singular key

**Files:**
- Modify: `config/config_loader.py`
- Test: `tests/unit/test_trigger_labels_migration.py`

- [ ] **Step 1: Find the JiraConfig load site**

Run: `grep -n "JiraConfig\|trigger_label" config/config_loader.py`

Expected: shows the function that builds `JiraConfig` from dict data (let's call it `_build_jira_config` — confirm exact name, might be `_parse_jira` or inline in project loader).

- [ ] **Step 2: Add failing test**

Append to `tests/unit/test_trigger_labels_migration.py`:

```python
from pathlib import Path

from config.config_loader import ConfigError, load_project_config


def test_loader_rejects_legacy_trigger_label(tmp_path: Path):
    project_dir = tmp_path / "projects" / "legacy"
    project_dir.mkdir(parents=True)
    (project_dir / "project.yaml").write_text(
        """
project:
  id: "legacy"
  name: "Legacy"
  enabled: true

jira:
  url: "https://example.com"
  token: "${X}"
  email: "x@example.com"
  project_key: "LEG"
  trigger_label: "ai-pipeline"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="trigger_label.*trigger_labels"):
        load_project_config(tmp_path, "legacy")
```

(Adjust `load_project_config` import to match the actual loader API — verify with `grep -n "^def " config/config_loader.py` before writing.)

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_trigger_labels_migration.py::test_loader_rejects_legacy_trigger_label -v`
Expected: FAIL — either silently passes (no error) or error message doesn't match.

- [ ] **Step 4: Add the migration guard in the loader**

In the function that parses the `jira:` block of `project.yaml`, before constructing `JiraConfig`, add:

```python
if "trigger_label" in jira_dict:
    raise ConfigError(
        "Legacy 'trigger_label' (singular) is no longer supported. "
        "Rename to 'trigger_labels' and make it a list, e.g. "
        "trigger_labels: [\"ai-pipeline\"]",
        file_path=str(project_yaml_path),
        field="jira.trigger_label",
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_trigger_labels_migration.py -v`
Expected: PASS (both tests)

- [ ] **Step 6: Commit**

```bash
git add config/config_loader.py tests/unit/test_trigger_labels_migration.py
git commit -m "feat(config): reject legacy singular trigger_label with migration error"
```

---

## Task 3: Update `filter_tickets` to require all trigger labels

**Files:**
- Modify: `orchestrator/ticket_prioritizer.py:26-42, 185-190`
- Test: `tests/unit/test_ticket_prioritizer.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/unit/test_ticket_prioritizer.py`:

```python
def test_filter_tickets_requires_all_trigger_labels(make_ticket):
    t1 = make_ticket(id="A-1", labels=["ai-pipeline", "acme-mobile"])
    t2 = make_ticket(id="A-2", labels=["ai-pipeline"])
    t3 = make_ticket(id="A-3", labels=["acme-mobile"])

    result = filter_tickets(
        [t1, t2, t3],
        trigger_labels=["ai-pipeline", "acme-mobile"],
        ignore_labels=[],
    )

    assert [t.id for t in result] == ["A-1"]


def test_filter_tickets_empty_trigger_labels_rejects_all(make_ticket):
    t1 = make_ticket(id="A-1", labels=["ai-pipeline"])
    result = filter_tickets([t1], trigger_labels=[], ignore_labels=[])
    assert result == []
```

(The existing `make_ticket` fixture is in the same test file. If tests currently pass `trigger_label="..."`, leave those — they'll be updated in the next step.)

- [ ] **Step 2: Update existing tests that call `filter_tickets` with singular**

In `tests/unit/test_ticket_prioritizer.py`, find every call to `filter_tickets(..., trigger_label="ai-pipeline", ...)` and change to `trigger_labels=["ai-pipeline"]`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_ticket_prioritizer.py -v`
Expected: FAIL — `TypeError: filter_tickets() got an unexpected keyword argument 'trigger_labels'`

- [ ] **Step 4: Update `filter_tickets`**

Edit `orchestrator/ticket_prioritizer.py` lines 26–42:

```python
def filter_tickets(
    tickets: list[TicketData],
    trigger_labels: list[str],
    ignore_labels: list[str],
    bot_name: str = "Cleave Pipeline",
) -> list[TicketData]:
    if not trigger_labels:
        return []

    result = []
    for ticket in tickets:
        # Must have ALL trigger labels
        missing = [l for l in trigger_labels if l not in ticket.labels]
        if missing:
            logger.debug(
                "Skipping %s: missing trigger labels %s", ticket.id, missing
            )
            continue

        # Must not have any ignore labels
```

And update the call site at lines 185–190:

```python
filtered = filter_tickets(
    tickets,
    trigger_labels=jira_config.trigger_labels,
    ignore_labels=jira_config.ignore_labels,
    bot_name=bot_name,
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_ticket_prioritizer.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add orchestrator/ticket_prioritizer.py tests/unit/test_ticket_prioritizer.py
git commit -m "feat(prioritizer): require all trigger_labels (AND semantics)"
```

---

## Task 4: Update Jira adapter to AND-filter

**Files:**
- Modify: `integrations/jira/jira_adapter.py:25-36, 80-85`
- Test: `tests/integration/test_jira_adapter.py`

- [ ] **Step 1: Update existing integration tests for plural field**

In `tests/integration/test_jira_adapter.py`, change every `trigger_label="..."` constructor arg and every assertion that references `trigger_label` to use `trigger_labels=[...]`.

- [ ] **Step 2: Add a failing test for AND JQL**

Append to `tests/integration/test_jira_adapter.py`:

```python
def test_build_jql_ands_multiple_trigger_labels():
    adapter = JiraAdapter(
        url="https://example.atlassian.net",
        email="bot@example.com",
        token="tok",
        project_key="ACME",
        trigger_labels=["ai-pipeline", "acme-mobile"],
    )
    jql = adapter._build_todo_jql()
    assert 'labels = "ai-pipeline"' in jql
    assert 'labels = "acme-mobile"' in jql
    assert jql.count("AND") >= 3  # project AND label1 AND label2 AND status
```

(If `_build_todo_jql` doesn't exist as a helper, refactor the JQL-building code out of the existing method into one during Step 3 so it's independently testable.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/integration/test_jira_adapter.py -v`
Expected: FAIL — constructor keyword mismatch.

- [ ] **Step 4: Update the adapter**

Edit `integrations/jira/jira_adapter.py` lines 25–36:

```python
    def __init__(
        self,
        url: str,
        email: str,
        token: str,
        project_key: str,
        trigger_labels: list[str] | None = None,
        ignore_labels: list[str] | None = None,
        statuses: dict[str, str] | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._project_key = project_key
        self._trigger_labels = trigger_labels or ["ai-pipeline"]
        self._ignore_labels = ignore_labels or []
```

Extract the JQL builder to a helper (lines ~80–85 become a call):

```python
    def _build_todo_jql(self) -> str:
        label_clauses = " AND ".join(
            f'labels = "{l}"' for l in self._trigger_labels
        )
        jql = (
            f'project = {self._project_key} '
            f'AND {label_clauses} '
            f'AND status = "{self._statuses["todo"]}"'
        )
        ignore = ", ".join(f'"{l}"' for l in self._ignore_labels)
        if ignore:
            jql += f" AND labels NOT IN ({ignore})"
        return jql
```

And replace the inline JQL construction with `jql = self._build_todo_jql()`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/integration/test_jira_adapter.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add integrations/jira/jira_adapter.py tests/integration/test_jira_adapter.py
git commit -m "feat(jira): AND-filter tickets across multiple trigger_labels"
```

---

## Task 5: Migrate acme and fixture configs

**Files:**
- Modify: `config-live/projects/acme/project.yaml`
- Modify: `config-live.example/projects/example-project/project.yaml`
- Modify: `tests/fixtures/config/projects/test-project/project.yaml`
- Modify: `tests/unit/test_config_cascade.py`

- [ ] **Step 1: Inspect acme config**

Run: `grep -n trigger_label config-live/projects/acme/project.yaml`

Expected: one line like `  trigger_label: "ai-pipeline"`.

- [ ] **Step 2: Rewrite acme config**

Edit `config-live/projects/acme/project.yaml` — change `trigger_label: "ai-pipeline"` to:

```yaml
trigger_labels: ["ai-pipeline"]
```

(If acme uses a different label value, preserve that value inside the list.)

- [ ] **Step 3: Rewrite example and fixture configs**

Same substitution in:

- `config-live.example/projects/example-project/project.yaml`
- `tests/fixtures/config/projects/test-project/project.yaml`

- [ ] **Step 4: Update test_config_cascade**

In `tests/unit/test_config_cascade.py`, find every reference to `trigger_label` (field assertions, fixture strings) and change to `trigger_labels` / list form.

- [ ] **Step 5: Run the full unit + integration suite for Phase 1**

Run: `pytest tests/unit/test_config_cascade.py tests/unit/test_ticket_prioritizer.py tests/unit/test_trigger_labels_migration.py tests/integration/test_jira_adapter.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add config-live/projects/acme/project.yaml \
  config-live.example/projects/example-project/project.yaml \
  tests/fixtures/config/projects/test-project/project.yaml \
  tests/unit/test_config_cascade.py
git commit -m "chore(config): migrate acme and fixtures to trigger_labels plural"
```

---

## Task 6: Update docs referencing `trigger_label`

**Files:**
- Modify: `docs/architecture.md`, `docs/architecture-v2.md`, `docs/prd.md`, `docs/agent-contracts.md`, `docs/setup-guide.md`, `docs/features/jira-integration.md`, `agents/project-setup-agent.md`, `agents/pm-agent.md`

- [ ] **Step 1: Find all doc references**

Run: `grep -rln "trigger_label" docs/ agents/`

- [ ] **Step 2: For each file, replace `trigger_label` with `trigger_labels` and update surrounding prose**

For each hit: change YAML snippets to the list form, change prose from "the trigger label" to "the trigger labels". In `agents/project-setup-agent.md` Phase 2 step 7, change the question from asking for a single label to asking for "one or more trigger labels (comma-separated)".

- [ ] **Step 3: Verify no stale references remain**

Run: `grep -rn "trigger_label[^s]" docs/ agents/ config/ orchestrator/ integrations/`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add docs/ agents/
git commit -m "docs: update trigger_label → trigger_labels across docs and agent prompts"
```

---

# Phase 2 — Secret writer + setup workspace

## Task 7: Write `env_writer` module

**Files:**
- Create: `dashboard/env_writer.py`
- Create: `tests/unit/test_env_writer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_env_writer.py`:

```python
import os
import stat
from pathlib import Path

import pytest

from dashboard.env_writer import (
    EnvCollisionError,
    append_vars,
    read_existing_vars,
    remove_vars,
)


def test_read_existing_vars_parses_simple_assignments(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
    assert read_existing_vars(env_path) == {"FOO", "BAZ"}


def test_read_existing_vars_ignores_comments_and_blanks(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("# comment\n\nFOO=bar\n", encoding="utf-8")
    assert read_existing_vars(env_path) == {"FOO"}


def test_read_existing_vars_missing_file_returns_empty(tmp_path: Path):
    assert read_existing_vars(tmp_path / ".env") == set()


def test_append_vars_atomic_write_and_chmod(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\n", encoding="utf-8")
    append_vars(env_path, {"ACME_JIRA_TOKEN": "abc", "ACME_GITHUB_TOKEN": "def"})
    content = env_path.read_text(encoding="utf-8")
    assert "EXISTING=1" in content
    assert "ACME_JIRA_TOKEN=abc" in content
    assert "ACME_GITHUB_TOKEN=def" in content
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600


def test_append_vars_creates_file_if_missing(tmp_path: Path):
    env_path = tmp_path / ".env"
    append_vars(env_path, {"X": "y"})
    assert env_path.read_text(encoding="utf-8") == "X=y\n"


def test_append_vars_raises_on_collision(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("ACME_JIRA_TOKEN=old\n", encoding="utf-8")
    with pytest.raises(EnvCollisionError) as exc:
        append_vars(env_path, {"ACME_JIRA_TOKEN": "new"})
    assert exc.value.vars == ["ACME_JIRA_TOKEN"]


def test_remove_vars_deletes_lines(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=1\nACME_JIRA_TOKEN=abc\nBAR=2\n", encoding="utf-8")
    remove_vars(env_path, ["ACME_JIRA_TOKEN"])
    assert env_path.read_text(encoding="utf-8") == "FOO=1\nBAR=2\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_env_writer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dashboard.env_writer'`

- [ ] **Step 3: Write the module**

Create `dashboard/env_writer.py`:

```python
"""Atomic .env file writer for the project-create wizard.

Writes raw secret assignments to the repo-root .env file that run.sh sources
at startup. All writes are atomic (tempfile + rename) and the resulting file
permissions are 0600.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


@dataclass
class EnvCollisionError(Exception):
    vars: list[str]

    def __str__(self) -> str:
        return f"env vars already defined: {', '.join(self.vars)}"


def read_existing_vars(env_path: Path) -> set[str]:
    if not env_path.exists():
        return set()
    names: set[str] = set()
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ASSIGN_RE.match(stripped)
        if m:
            names.add(m.group(1))
    return names


def _atomic_write(env_path: Path, content: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".env.", dir=str(env_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, env_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def append_vars(env_path: Path, vars: dict[str, str]) -> None:
    existing = read_existing_vars(env_path)
    collisions = sorted(name for name in vars if name in existing)
    if collisions:
        raise EnvCollisionError(vars=collisions)

    prefix = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    addition = "".join(f"{name}={value}\n" for name, value in vars.items())
    _atomic_write(env_path, prefix + addition)


def remove_vars(env_path: Path, names: list[str]) -> None:
    if not env_path.exists():
        return
    to_remove = set(names)
    kept: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        m = _ASSIGN_RE.match(line.strip())
        if m and m.group(1) in to_remove:
            continue
        kept.append(line)
    content = "\n".join(kept)
    if content and not content.endswith("\n"):
        content += "\n"
    _atomic_write(env_path, content)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_env_writer.py -v`
Expected: PASS (all 7)

- [ ] **Step 5: Commit**

```bash
git add dashboard/env_writer.py tests/unit/test_env_writer.py
git commit -m "feat(dashboard): add atomic .env writer with collision detection"
```

---

## Task 8: Write `setup_workspace` module

**Files:**
- Create: `dashboard/setup_workspace.py`
- Create: `tests/unit/test_setup_workspace.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_setup_workspace.py`:

```python
import json
from pathlib import Path

from dashboard.setup_workspace import (
    create_setup_workspace,
    setup_workspace_path,
    write_state,
)


def test_setup_workspace_path_matches_ticket_shape(tmp_path: Path):
    p = setup_workspace_path(tmp_path, "acme", "acme-app")
    assert p == tmp_path / "acme" / "acme-app" / "setup"


def test_create_setup_workspace_creates_tree(tmp_path: Path):
    workspace = create_setup_workspace(
        base_dir=tmp_path,
        project_id="acme",
        repo_id="acme-app",
        redacted_input_md="# Project Setup Input\n- project_id: acme\n",
    )
    assert workspace.setup_dir.is_dir()
    assert workspace.tickets_dir.is_dir()
    assert (workspace.setup_dir / "meta" / "input.md").read_text().startswith(
        "# Project Setup Input"
    )
    assert (workspace.setup_dir / "logs").is_dir()
    state = json.loads((workspace.setup_dir / "state.json").read_text())
    assert state["current_state"] == "SETUP_PENDING"
    assert state["ticket_id"] == "setup"
    assert state["company_id"] == "acme"
    assert state["repo_id"] == "acme-app"


def test_write_state_transitions(tmp_path: Path):
    workspace = create_setup_workspace(
        base_dir=tmp_path,
        project_id="acme",
        repo_id="acme-app",
        redacted_input_md="",
    )
    write_state(workspace, "VALIDATING")
    state = json.loads((workspace.setup_dir / "state.json").read_text())
    assert state["current_state"] == "VALIDATING"
    assert state["previous_state"] == "SETUP_PENDING"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_setup_workspace.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the module**

Create `dashboard/setup_workspace.py`:

```python
"""Create and update the setup workspace tree for project-create runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SetupWorkspace:
    project_id: str
    repo_id: str
    project_dir: Path
    repo_dir: Path
    tickets_dir: Path
    setup_dir: Path


def setup_workspace_path(base_dir: Path, project_id: str, repo_id: str) -> Path:
    return Path(base_dir) / project_id / repo_id / "setup"


def create_setup_workspace(
    base_dir: Path,
    project_id: str,
    repo_id: str,
    redacted_input_md: str,
) -> SetupWorkspace:
    base = Path(base_dir)
    project_dir = base / project_id
    repo_dir = project_dir / repo_id
    tickets_dir = repo_dir / "tickets"
    setup_dir = repo_dir / "setup"

    tickets_dir.mkdir(parents=True, exist_ok=True)
    (setup_dir / "meta").mkdir(parents=True, exist_ok=True)
    (setup_dir / "reports").mkdir(parents=True, exist_ok=True)
    (setup_dir / "logs").mkdir(parents=True, exist_ok=True)

    (setup_dir / "meta" / "input.md").write_text(redacted_input_md, encoding="utf-8")

    now = datetime.now(timezone.utc).isoformat()
    state = {
        "ticket_id": "setup",
        "company_id": project_id,
        "repo_id": repo_id,
        "current_state": "SETUP_PENDING",
        "previous_state": None,
        "started_at": now,
        "last_updated_at": now,
        "kind": "setup",
    }
    (setup_dir / "state.json").write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )

    return SetupWorkspace(
        project_id=project_id,
        repo_id=repo_id,
        project_dir=project_dir,
        repo_dir=repo_dir,
        tickets_dir=tickets_dir,
        setup_dir=setup_dir,
    )


def write_state(
    workspace: SetupWorkspace,
    new_state: str,
    error: str | None = None,
) -> None:
    state_path = workspace.setup_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["previous_state"] = state.get("current_state")
    state["current_state"] = new_state
    state["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    if error is not None:
        state["error"] = error
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_setup_workspace.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add dashboard/setup_workspace.py tests/unit/test_setup_workspace.py
git commit -m "feat(dashboard): add setup workspace creator with state transitions"
```

---

# Phase 3 — Payload validation + redaction

## Task 9: Payload schema + validator

**Files:**
- Create: `dashboard/project_create_payload.py`
- Create: `tests/unit/test_project_create_payload.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_project_create_payload.py`:

```python
import pytest

from dashboard.project_create_payload import (
    PayloadValidationError,
    derive_env_vars,
    redact_to_input_md,
    validate_payload,
)


VALID_PAYLOAD = {
    "identity": {
        "project_id": "acme",
        "display_name": "Acme Corp",
        "repo_id": "acme-app",
        "repo_display_name": "Acme App",
    },
    "jira": {
        "url": "https://acme.atlassian.net",
        "project_key": "ACME",
        "email": "bot@acme.com",
        "token": "jira-raw",
        "trigger_labels": ["ai-pipeline", "acme-app"],
        "ignore_labels": [],
        "statuses": {
            "todo": "To Do",
            "in_progress": "In Progress",
            "in_review": "In Review",
            "done": "Done",
        },
    },
    "vcs": {
        "provider": "github",
        "github": {
            "owner": "acme",
            "repo": "acme-app",
            "token": "gh-raw",
            "default_branch": "develop",
            "branch_prefix": "feature",
            "merge_method": "squash",
        },
    },
    "quality": {
        "lint":  {"command": "npm run lint",  "hard_gate": True},
        "test":  {"command": "npm test",      "hard_gate": True},
        "build": {"command": "npm run build", "hard_gate": True},
    },
    "extras": {
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "arch_rules_file": "docs/arch-rules.md",
        "protected_files": [],
        "max_concurrent_tickets": None,
    },
}


def test_validate_payload_happy_path():
    validate_payload(VALID_PAYLOAD)  # does not raise


def test_validate_payload_rejects_missing_project_id():
    p = {**VALID_PAYLOAD, "identity": {**VALID_PAYLOAD["identity"]}}
    del p["identity"]["project_id"]
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    assert "identity.project_id" in exc.value.field_errors


def test_validate_payload_rejects_bad_slug():
    p = {**VALID_PAYLOAD, "identity": {**VALID_PAYLOAD["identity"], "project_id": "Bad Slug!"}}
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    assert "identity.project_id" in exc.value.field_errors


def test_validate_payload_rejects_empty_trigger_labels():
    p = {**VALID_PAYLOAD, "jira": {**VALID_PAYLOAD["jira"], "trigger_labels": []}}
    with pytest.raises(PayloadValidationError) as exc:
        validate_payload(p)
    assert "jira.trigger_labels" in exc.value.field_errors


def test_validate_payload_rejects_bad_vcs_provider():
    p = {**VALID_PAYLOAD, "vcs": {**VALID_PAYLOAD["vcs"], "provider": "bitbucket"}}
    with pytest.raises(PayloadValidationError):
        validate_payload(p)


def test_derive_env_vars_github():
    vars = derive_env_vars(VALID_PAYLOAD)
    assert vars == {
        "ACME_JIRA_TOKEN": "jira-raw",
        "ACME_GITHUB_TOKEN": "gh-raw",
    }


def test_derive_env_vars_includes_telegram_when_provided():
    p = {**VALID_PAYLOAD}
    p["extras"] = {**VALID_PAYLOAD["extras"], "telegram_bot_token": "tg-raw"}
    vars = derive_env_vars(p)
    assert vars["ACME_TELEGRAM_BOT_TOKEN"] == "tg-raw"


def test_redact_to_input_md_contains_var_names_not_secrets():
    md = redact_to_input_md(VALID_PAYLOAD)
    assert "jira-raw" not in md
    assert "gh-raw" not in md
    assert "ACME_JIRA_TOKEN" in md
    assert "ACME_GITHUB_TOKEN" in md
    assert "trigger_labels: [ai-pipeline, acme-app]" in md
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_project_create_payload.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the module**

Create `dashboard/project_create_payload.py`:

```python
"""Payload validation and transformation for POST /api/projects/create."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
VCS_PROVIDERS = {"github", "gitlab"}


@dataclass
class PayloadValidationError(Exception):
    field_errors: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return "; ".join(f"{k}: {v}" for k, v in self.field_errors.items())


def _require(errors: dict[str, str], obj: dict, key: str, path: str) -> Any:
    if key not in obj or obj[key] in ("", None):
        errors[path] = "required"
        return None
    return obj[key]


def _require_slug(errors: dict[str, str], value: Any, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not SLUG_RE.match(value):
        errors[path] = "must be lowercase alnum/hyphen slug starting with a letter"


def validate_payload(payload: dict[str, Any]) -> None:
    errors: dict[str, str] = {}

    identity = payload.get("identity") or {}
    pid = _require(errors, identity, "project_id", "identity.project_id")
    _require_slug(errors, pid, "identity.project_id")
    _require(errors, identity, "display_name", "identity.display_name")
    rid = _require(errors, identity, "repo_id", "identity.repo_id")
    _require_slug(errors, rid, "identity.repo_id")
    _require(errors, identity, "repo_display_name", "identity.repo_display_name")

    jira = payload.get("jira") or {}
    _require(errors, jira, "url", "jira.url")
    _require(errors, jira, "project_key", "jira.project_key")
    _require(errors, jira, "email", "jira.email")
    _require(errors, jira, "token", "jira.token")
    labels = jira.get("trigger_labels")
    if not isinstance(labels, list) or len(labels) == 0:
        errors["jira.trigger_labels"] = "must be a non-empty list"

    vcs = payload.get("vcs") or {}
    provider = vcs.get("provider")
    if provider not in VCS_PROVIDERS:
        errors["vcs.provider"] = f"must be one of {sorted(VCS_PROVIDERS)}"
    elif provider == "github":
        gh = vcs.get("github") or {}
        _require(errors, gh, "owner", "vcs.github.owner")
        _require(errors, gh, "repo", "vcs.github.repo")
        _require(errors, gh, "token", "vcs.github.token")
    elif provider == "gitlab":
        gl = vcs.get("gitlab") or {}
        _require(errors, gl, "url", "vcs.gitlab.url")
        _require(errors, gl, "project_id", "vcs.gitlab.project_id")
        _require(errors, gl, "token", "vcs.gitlab.token")

    if errors:
        raise PayloadValidationError(field_errors=errors)


def derive_env_vars(payload: dict[str, Any]) -> dict[str, str]:
    project_id = payload["identity"]["project_id"]
    prefix = project_id.upper().replace("-", "_")
    vars: dict[str, str] = {
        f"{prefix}_JIRA_TOKEN": payload["jira"]["token"],
    }
    vcs = payload["vcs"]
    provider = vcs["provider"]
    if provider == "github":
        vars[f"{prefix}_GITHUB_TOKEN"] = vcs["github"]["token"]
    else:
        vars[f"{prefix}_GITLAB_TOKEN"] = vcs["gitlab"]["token"]

    tg = (payload.get("extras") or {}).get("telegram_bot_token")
    if tg:
        vars[f"{prefix}_TELEGRAM_BOT_TOKEN"] = tg

    return vars


def redact_to_input_md(payload: dict[str, Any]) -> str:
    project_id = payload["identity"]["project_id"]
    prefix = project_id.upper().replace("-", "_")
    identity = payload["identity"]
    jira = payload["jira"]
    vcs = payload["vcs"]
    quality = payload.get("quality") or {}
    extras = payload.get("extras") or {}

    lines: list[str] = ["# Project Setup Input", "", "## Identity"]
    lines.append(f"- project_id: {identity['project_id']}")
    lines.append(f"- display_name: {identity['display_name']}")
    lines.append(f"- repo_id: {identity['repo_id']}")
    lines.append(f"- repo_display_name: {identity['repo_display_name']}")

    lines += ["", "## Jira"]
    lines.append(f"- url: {jira['url']}")
    lines.append(f"- project_key: {jira['project_key']}")
    lines.append(f"- email: {jira['email']}")
    lines.append(f"- token_var: {prefix}_JIRA_TOKEN")
    lines.append(
        "- trigger_labels: [" + ", ".join(jira["trigger_labels"]) + "]"
    )
    lines.append(
        "- ignore_labels: [" + ", ".join(jira.get("ignore_labels") or []) + "]"
    )
    statuses = jira.get("statuses") or {}
    lines.append(f"- statuses: {statuses}")

    lines += ["", "## VCS"]
    provider = vcs["provider"]
    lines.append(f"- provider: {provider}")
    if provider == "github":
        gh = vcs["github"]
        lines.append(f"- owner: {gh['owner']}")
        lines.append(f"- repo: {gh['repo']}")
        lines.append(f"- token_var: {prefix}_GITHUB_TOKEN")
        lines.append(f"- default_branch: {gh.get('default_branch', 'develop')}")
        lines.append(f"- branch_prefix: {gh.get('branch_prefix', 'feature')}")
        lines.append(f"- merge_method: {gh.get('merge_method', 'squash')}")
    else:
        gl = vcs["gitlab"]
        lines.append(f"- url: {gl['url']}")
        lines.append(f"- project_id: {gl['project_id']}")
        lines.append(f"- token_var: {prefix}_GITLAB_TOKEN")
        lines.append(f"- default_branch: {gl.get('default_branch', 'develop')}")
        lines.append(f"- branch_prefix: {gl.get('branch_prefix', 'feature')}")

    lines += ["", "## Quality"]
    for key in ("lint", "test", "build"):
        q = quality.get(key)
        if q and q.get("command"):
            lines.append(f"- {key}: {{command: \"{q['command']}\", hard_gate: {q.get('hard_gate', True)}}}")

    lines += ["", "## Extras"]
    tg_var = f"{prefix}_TELEGRAM_BOT_TOKEN" if extras.get("telegram_bot_token") else None
    lines.append(f"- telegram_bot_token_var: {tg_var}")
    lines.append(f"- telegram_chat_id: {extras.get('telegram_chat_id')}")
    lines.append(f"- arch_rules_file: {extras.get('arch_rules_file')}")
    lines.append(f"- protected_files: [" + ", ".join(extras.get("protected_files") or []) + "]")
    lines.append(f"- max_concurrent_tickets: {extras.get('max_concurrent_tickets')}")

    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_project_create_payload.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add dashboard/project_create_payload.py tests/unit/test_project_create_payload.py
git commit -m "feat(dashboard): add project-create payload validation and redaction"
```

---

# Phase 4 — Atlas orchestrator mode

## Task 10: Atlas agent prompt — orchestrator mode

**Files:**
- Modify: `agents/project-setup-agent.md`

The Atlas agent already supports CLI mode with interactive Q&A. This task adds an **orchestrator mode** where Atlas reads `meta/input.md`, skips all questions, validates, and writes.

- [ ] **Step 1: Append an "Operation: Add (orchestrator mode)" section**

In `agents/project-setup-agent.md`, after the existing `## Operation: Add` section, add:

```markdown
## Operation: Add (orchestrator mode)

When `meta/input.md` exists in the current workspace, Atlas MUST NOT ask
questions. Instead:

1. Read `meta/input.md` and parse the key-value pairs under each section.
2. Read env vars referenced as `token_var:` — they are already set in the
   process environment by the dashboard handler.
3. For each credential, call the matching validator (`validate_jira`,
   `validate_github` or `validate_gitlab`). On failure, write
   `reports/project-setup-output.md` with the validation error and raise —
   the supervising handler will transition state to `SETUP_FAILED`.
4. Write `config-live/projects/{project_id}/project.yaml` and
   `config-live/projects/{project_id}/repos/{repo_id}.yaml` using
   `${TOKEN_VAR}` references (never raw values). Use `trigger_labels` (plural
   list) in the Jira block.
5. Do NOT write a `ci:` block (CI/CD is out of scope for the web flow).
6. Write `reports/project-setup-output.md` with a summary of what was created,
   including the list of env var names that must remain set for the project
   to operate.
```

- [ ] **Step 2: Update Phase 2 step 7 to use plural labels**

In the same file, change the existing CLI-mode step 7 prompt from "trigger label (default: ai-pipeline)" to "trigger labels (comma-separated, default: ai-pipeline)". Update the YAML skeleton in step 28 to use `trigger_labels: [...]`.

- [ ] **Step 3: Commit**

```bash
git add agents/project-setup-agent.md
git commit -m "feat(agent): add Atlas orchestrator-mode flow for dashboard handoff"
```

---

## Task 11: Atlas runner (background task supervisor)

**Files:**
- Create: `dashboard/atlas_runner.py`

Note: Testing this module end-to-end happens in Task 14 (integration test). This task writes the runner against a `run_atlas` callable that can be substituted in tests.

**State transition contract:** `run_supervised` emits `VALIDATING` before calling `atlas_fn` and `SETUP_DONE` after. The `WRITING` transition is the responsibility of `atlas_fn` itself — it should call `write_state(workspace, "WRITING")` after credential validation passes and before writing YAMLs. The production Atlas runner in Task 23 does this; the fake atlas functions in tests may skip it (the integration test asserts either `VALIDATING` or `WRITING` as the last `previous_state`).

- [ ] **Step 1: Write the module**

Create `dashboard/atlas_runner.py`:

```python
"""Spawn and supervise the Atlas project-setup agent as a background task."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path

from dashboard.setup_workspace import SetupWorkspace, write_state

logger = logging.getLogger(__name__)

AtlasFn = Callable[[SetupWorkspace, Path], Awaitable[None]]


async def run_supervised(
    workspace: SetupWorkspace,
    config_dir: Path,
    atlas_fn: AtlasFn,
    on_failure: Callable[[], None],
) -> None:
    """Run Atlas in VALIDATING → WRITING → SETUP_DONE.

    On any exception, writes SETUP_FAILED with the exception, appends a
    failure report to reports/project-setup-output.md, and calls on_failure
    for rollback (removing env vars, partial configs, etc.).
    """
    try:
        write_state(workspace, "VALIDATING")
        await atlas_fn(workspace, config_dir)
        write_state(workspace, "SETUP_DONE")
    except Exception as exc:
        logger.exception("Atlas run failed for %s/%s",
                         workspace.project_id, workspace.repo_id)
        report = workspace.setup_dir / "reports" / "project-setup-output.md"
        existing = report.read_text(encoding="utf-8") if report.exists() else ""
        report.write_text(
            existing
            + "\n\n## Failure\n\n"
            + f"```\n{traceback.format_exc()}\n```\n",
            encoding="utf-8",
        )
        write_state(workspace, "SETUP_FAILED", error=str(exc))
        try:
            on_failure()
        except Exception:
            logger.exception("Rollback failed for %s/%s",
                             workspace.project_id, workspace.repo_id)


def schedule(
    workspace: SetupWorkspace,
    config_dir: Path,
    atlas_fn: AtlasFn,
    on_failure: Callable[[], None],
) -> asyncio.Task:
    return asyncio.create_task(
        run_supervised(workspace, config_dir, atlas_fn, on_failure)
    )
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/atlas_runner.py
git commit -m "feat(dashboard): add supervised Atlas runner for project-create"
```

---

# Phase 5 — POST /api/projects/create handler

## Task 12: Project create handler

**Files:**
- Create: `dashboard/project_create.py`

- [ ] **Step 1: Write the module**

Create `dashboard/project_create.py`:

```python
"""POST /api/projects/create handler.

Wires together payload validation, .env writes, workspace creation,
Atlas supervision, and rollback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

from dashboard.atlas_runner import AtlasFn, schedule
from dashboard.env_writer import EnvCollisionError, append_vars, remove_vars
from dashboard.project_create_payload import (
    PayloadValidationError,
    derive_env_vars,
    redact_to_input_md,
    validate_payload,
)
from dashboard.setup_workspace import create_setup_workspace

logger = logging.getLogger(__name__)

_run_lock = asyncio.Lock()
_active_workspace: str | None = None


def build_create_route(
    *,
    workspace_base_dir: Path,
    config_dir: Path,
    env_path: Path,
    atlas_fn: AtlasFn,
):
    async def create_project(request: Request) -> JSONResponse:
        global _active_workspace

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        try:
            validate_payload(payload)
        except PayloadValidationError as exc:
            return JSONResponse(
                {"error": "validation_failed", "fields": exc.field_errors},
                status_code=400,
            )

        project_id = payload["identity"]["project_id"]
        repo_id = payload["identity"]["repo_id"]

        if _run_lock.locked():
            return JSONResponse(
                {"error": "busy", "active_workspace": _active_workspace},
                status_code=429,
            )

        async with _run_lock:
            project_config_dir = config_dir / "projects" / project_id
            if project_config_dir.exists():
                return JSONResponse(
                    {"error": "project_exists", "project_id": project_id},
                    status_code=409,
                )

            env_vars = derive_env_vars(payload)
            try:
                append_vars(env_path, env_vars)
            except EnvCollisionError as exc:
                return JSONResponse(
                    {"error": "env_var_conflict", "vars": exc.vars},
                    status_code=409,
                )

            for name, value in env_vars.items():
                os.environ[name] = value

            workspace = create_setup_workspace(
                base_dir=workspace_base_dir,
                project_id=project_id,
                repo_id=repo_id,
                redacted_input_md=redact_to_input_md(payload),
            )

            _active_workspace = f"{project_id}/{repo_id}/setup"

            def rollback() -> None:
                if project_config_dir.exists():
                    shutil.rmtree(project_config_dir, ignore_errors=True)
                remove_vars(env_path, list(env_vars.keys()))
                for name in env_vars:
                    os.environ.pop(name, None)

            schedule(workspace, config_dir, atlas_fn, rollback)

        return JSONResponse(
            {
                "workspace": f"{project_id}/{repo_id}/setup",
                "state": "SETUP_PENDING",
            },
            status_code=202,
        )

    return create_project
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/project_create.py
git commit -m "feat(dashboard): add POST /api/projects/create handler"
```

---

## Task 13: Wire route in `dashboard/web.py` and tag setup workspaces

**Files:**
- Modify: `dashboard/web.py`

- [ ] **Step 1: Update `_scan_all_workspaces` to tag `kind`**

In `dashboard/web.py`, inside the `_scan_all_workspaces` function, after building the workspace dict, add the `kind` classification:

```python
            kind = "setup" if ws_root.name == "setup" else "ticket"
            results.append({
                "ticket_id": data.get("ticket_id", ""),
                "company_id": data.get("company_id", ""),
                # ... existing fields ...
                "kind": kind,
            })
```

- [ ] **Step 2: Register the create route**

In `create_app`, after the existing action-routes block, add:

```python
    # Project-create route
    from dashboard.project_create import build_create_route
    from dashboard.atlas_runner import AtlasFn  # noqa: F401
    from pathlib import Path

    async def _default_atlas_fn(workspace, config_dir):
        # Real implementation lives in orchestrator/agents; this is a seam
        # that main.py wires to the production runner.
        raise RuntimeError("atlas_fn not configured")

    create_route = build_create_route(
        workspace_base_dir=Path(workspace_base_dir),
        config_dir=Path(global_config.config_dir) if global_config else Path("config-live"),
        env_path=Path(".env"),
        atlas_fn=getattr(global_config, "atlas_fn", None) or _default_atlas_fn,
    )
    routes.append(Route("/api/projects/create", create_route, methods=["POST"]))
```

(Verify `global_config.config_dir` is the actual attribute name by checking `config/schemas.py` — adjust if it's `config_path` or similar. If `global_config` is `None` in tests, default to `Path("config-live")`.)

- [ ] **Step 3: Manual smoke-check the dashboard still starts**

Run: `python -c "from dashboard.web import create_app"` (should not raise)

- [ ] **Step 4: Commit**

```bash
git add dashboard/web.py
git commit -m "feat(dashboard): register POST /api/projects/create and tag setup workspaces"
```

---

## Task 14: End-to-end integration test

**Files:**
- Create: `tests/integration/test_project_create_flow.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_project_create_flow.py`:

```python
import asyncio
import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from dashboard.project_create import build_create_route
from dashboard.setup_workspace import SetupWorkspace


VALID_PAYLOAD = {
    "identity": {
        "project_id": "acme",
        "display_name": "Acme Corp",
        "repo_id": "acme-app",
        "repo_display_name": "Acme App",
    },
    "jira": {
        "url": "https://acme.atlassian.net",
        "project_key": "ACME",
        "email": "bot@acme.com",
        "token": "jira-raw",
        "trigger_labels": ["ai-pipeline"],
        "ignore_labels": [],
        "statuses": {
            "todo": "To Do",
            "in_progress": "In Progress",
            "in_review": "In Review",
            "done": "Done",
        },
    },
    "vcs": {
        "provider": "github",
        "github": {
            "owner": "acme",
            "repo": "acme-app",
            "token": "gh-raw",
            "default_branch": "develop",
            "branch_prefix": "feature",
            "merge_method": "squash",
        },
    },
    "quality": {
        "lint":  {"command": "npm run lint",  "hard_gate": True},
        "test":  {"command": "npm test",      "hard_gate": True},
        "build": {"command": "npm run build", "hard_gate": True},
    },
    "extras": {
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "arch_rules_file": None,
        "protected_files": [],
        "max_concurrent_tickets": None,
    },
}


def _make_app(tmp_path: Path, atlas_fn):
    route = build_create_route(
        workspace_base_dir=tmp_path / "workspace",
        config_dir=tmp_path / "config-live",
        env_path=tmp_path / ".env",
        atlas_fn=atlas_fn,
    )
    return Starlette(routes=[Route("/api/projects/create", route, methods=["POST"])])


def _wait_for_state(workspace_dir: Path, expected: str, timeout: float = 2.0) -> dict:
    import time
    state_path = workspace_dir / "state.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state["current_state"] == expected:
                return state
        time.sleep(0.05)
    raise AssertionError(
        f"Timed out waiting for state {expected}; last: "
        f"{state_path.read_text() if state_path.exists() else 'missing'}"
    )


def test_happy_path_writes_configs_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("ACME_JIRA_TOKEN", raising=False)
    monkeypatch.delenv("ACME_GITHUB_TOKEN", raising=False)

    async def fake_atlas(workspace: SetupWorkspace, config_dir: Path) -> None:
        # Simulate Atlas writing configs
        proj = config_dir / "projects" / workspace.project_id
        (proj / "repos").mkdir(parents=True)
        (proj / "project.yaml").write_text("project: {id: acme}\n")
        (proj / f"repos/{workspace.repo_id}.yaml").write_text("repo: {id: acme-app}\n")
        (workspace.setup_dir / "reports" / "project-setup-output.md").write_text(
            "# Setup complete\n"
        )

    app = _make_app(tmp_path, fake_atlas)
    client = TestClient(app)
    resp = client.post("/api/projects/create", json=VALID_PAYLOAD)
    assert resp.status_code == 202
    body = resp.json()
    assert body["workspace"] == "acme/acme-app/setup"

    workspace_dir = tmp_path / "workspace" / "acme" / "acme-app" / "setup"
    state = _wait_for_state(workspace_dir, "SETUP_DONE")
    assert state["previous_state"] == "WRITING" or state["previous_state"] == "VALIDATING"

    # Configs were written
    assert (tmp_path / "config-live" / "projects" / "acme" / "project.yaml").exists()
    # Env vars were appended
    env_content = (tmp_path / ".env").read_text()
    assert "ACME_JIRA_TOKEN=jira-raw" in env_content
    assert "ACME_GITHUB_TOKEN=gh-raw" in env_content
    # Secrets are NOT in input.md
    input_md = (workspace_dir / "meta" / "input.md").read_text()
    assert "jira-raw" not in input_md
    assert "ACME_JIRA_TOKEN" in input_md
    # Tickets dir was bootstrapped
    assert (tmp_path / "workspace" / "acme" / "acme-app" / "tickets").is_dir()


def test_atlas_failure_rolls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("ACME_JIRA_TOKEN", raising=False)

    async def failing_atlas(workspace, config_dir):
        # Partially write a config then fail
        proj = config_dir / "projects" / workspace.project_id
        proj.mkdir(parents=True)
        (proj / "project.yaml").write_text("partial\n")
        raise RuntimeError("simulated validation failure")

    app = _make_app(tmp_path, failing_atlas)
    client = TestClient(app)
    resp = client.post("/api/projects/create", json=VALID_PAYLOAD)
    assert resp.status_code == 202

    workspace_dir = tmp_path / "workspace" / "acme" / "acme-app" / "setup"
    _wait_for_state(workspace_dir, "SETUP_FAILED")

    # Config dir cleaned up
    assert not (tmp_path / "config-live" / "projects" / "acme").exists()
    # .env cleaned up
    env_content = (tmp_path / ".env").read_text() if (tmp_path / ".env").exists() else ""
    assert "ACME_JIRA_TOKEN" not in env_content
    # os.environ cleaned up
    import os
    assert "ACME_JIRA_TOKEN" not in os.environ
    # Failure report exists
    report = workspace_dir / "reports" / "project-setup-output.md"
    assert "simulated validation failure" in report.read_text()


def test_project_already_exists_returns_409(tmp_path):
    (tmp_path / "config-live" / "projects" / "acme").mkdir(parents=True)
    app = _make_app(tmp_path, lambda w, c: asyncio.sleep(0))
    client = TestClient(app)
    resp = client.post("/api/projects/create", json=VALID_PAYLOAD)
    assert resp.status_code == 409
    assert resp.json()["error"] == "project_exists"


def test_env_var_conflict_returns_409(tmp_path):
    (tmp_path / ".env").write_text("ACME_JIRA_TOKEN=old\n")
    app = _make_app(tmp_path, lambda w, c: asyncio.sleep(0))
    client = TestClient(app)
    resp = client.post("/api/projects/create", json=VALID_PAYLOAD)
    assert resp.status_code == 409
    assert resp.json()["error"] == "env_var_conflict"
    assert "ACME_JIRA_TOKEN" in resp.json()["vars"]


def test_bad_payload_returns_400(tmp_path):
    app = _make_app(tmp_path, lambda w, c: asyncio.sleep(0))
    client = TestClient(app)
    bad = {**VALID_PAYLOAD, "jira": {**VALID_PAYLOAD["jira"], "trigger_labels": []}}
    resp = client.post("/api/projects/create", json=bad)
    assert resp.status_code == 400
    assert "jira.trigger_labels" in resp.json()["fields"]


def test_second_concurrent_post_returns_429(tmp_path, monkeypatch):
    monkeypatch.delenv("ACME_JIRA_TOKEN", raising=False)
    monkeypatch.delenv("ACME_GITHUB_TOKEN", raising=False)

    gate = asyncio.Event()

    async def slow_atlas(workspace, config_dir):
        await gate.wait()  # hold the lock until test releases it

    app = _make_app(tmp_path, slow_atlas)
    client = TestClient(app)

    # First request returns 202 and holds the run lock.
    first = client.post("/api/projects/create", json=VALID_PAYLOAD)
    assert first.status_code == 202

    # Second request (different project id) should 429 — lock is held.
    second_payload = {**VALID_PAYLOAD, "identity": {**VALID_PAYLOAD["identity"], "project_id": "other", "repo_id": "other-app"}}
    second = client.post("/api/projects/create", json=second_payload)
    assert second.status_code == 429
    assert second.json()["error"] == "busy"

    # Release the first run so it completes and the module state clears.
    gate.set()
    workspace_dir = tmp_path / "workspace" / "acme" / "acme-app" / "setup"
    _wait_for_state(workspace_dir, "SETUP_DONE")
```

- [ ] **Step 2: Run the integration test**

Run: `pytest tests/integration/test_project_create_flow.py -v`
Expected: PASS (all 5)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_project_create_flow.py
git commit -m "test(integration): end-to-end project-create flow with rollback"
```

---

# Phase 6 — Frontend wizard

## Task 15: Add new-project button and modal shell

**Files:**
- Modify: `dashboard/static/index.html`
- Modify: `dashboard/static/style.css`

- [ ] **Step 1: Add the button**

In `dashboard/static/index.html`, locate the header/toolbar area (find the existing top-level nav) and insert:

```html
<button id="new-project-btn" class="btn-primary">+ New Project</button>
<div id="project-wizard-modal" class="modal hidden" role="dialog" aria-modal="true">
  <div class="modal-backdrop"></div>
  <div class="modal-panel">
    <header class="wizard-header">
      <h2 id="wizard-title">New Project</h2>
      <button id="wizard-close" class="icon-btn" aria-label="Close">×</button>
    </header>
    <nav class="wizard-steps" id="wizard-steps"></nav>
    <main class="wizard-body" id="wizard-body"></main>
    <footer class="wizard-footer">
      <button id="wizard-back" class="btn-secondary">Back</button>
      <button id="wizard-next" class="btn-primary">Next</button>
    </footer>
  </div>
</div>
<script type="module" src="/static/js/project-wizard.js"></script>
```

- [ ] **Step 2: Add styles**

Append to `dashboard/static/style.css`:

```css
.modal.hidden { display: none; }
.modal { position: fixed; inset: 0; z-index: 1000; }
.modal-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,.5); }
.modal-panel {
  position: relative;
  max-width: 720px;
  margin: 5vh auto;
  background: #fff;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  max-height: 90vh;
  box-shadow: 0 20px 40px rgba(0,0,0,.2);
}
.wizard-header { display: flex; justify-content: space-between; padding: 16px 24px; border-bottom: 1px solid #eee; }
.wizard-steps { display: flex; gap: 8px; padding: 12px 24px; border-bottom: 1px solid #eee; flex-wrap: wrap; }
.wizard-steps .step { padding: 4px 12px; border-radius: 12px; background: #eee; font-size: 12px; }
.wizard-steps .step.active { background: #2563eb; color: #fff; }
.wizard-steps .step.done { background: #bbf7d0; }
.wizard-body { padding: 24px; overflow-y: auto; flex: 1; }
.wizard-footer { display: flex; justify-content: space-between; padding: 16px 24px; border-top: 1px solid #eee; }
.form-field { display: flex; flex-direction: column; margin-bottom: 16px; }
.form-field label { font-weight: 600; margin-bottom: 4px; }
.form-field input, .form-field select, .form-field textarea {
  padding: 8px; border: 1px solid #ccc; border-radius: 4px;
}
.form-field .error { color: #c00; font-size: 12px; margin-top: 4px; }
.chip-input { display: flex; flex-wrap: wrap; gap: 4px; padding: 6px; border: 1px solid #ccc; border-radius: 4px; }
.chip { background: #e0e7ff; border-radius: 12px; padding: 2px 8px; display: inline-flex; align-items: center; gap: 4px; }
.chip button { background: none; border: none; cursor: pointer; }
.chip-input input { flex: 1; border: none; outline: none; min-width: 80px; }
.status-panel { text-align: center; padding: 40px; }
.status-panel .status-step { display: inline-block; padding: 8px 16px; margin: 4px; border-radius: 4px; background: #eee; }
.status-panel .status-step.active { background: #2563eb; color: #fff; }
.status-panel .status-step.done { background: #bbf7d0; }
.status-panel.failed { color: #c00; }
```

- [ ] **Step 3: Wire the open button**

In `dashboard/static/js/app.js`, add at the end (or in the init function):

```javascript
document.getElementById('new-project-btn')?.addEventListener('click', () => {
  import('./project-wizard.js').then(({ openWizard }) => openWizard());
});
```

- [ ] **Step 4: Commit**

```bash
git add dashboard/static/index.html dashboard/static/style.css dashboard/static/js/app.js
git commit -m "feat(dashboard): add new-project button and wizard modal shell"
```

---

## Task 16: Wizard state machine and step framework

**Files:**
- Create: `dashboard/static/js/project-wizard.js`
- Modify: `dashboard/static/js/api.js`

- [ ] **Step 1: Add the API helper**

Append to `dashboard/static/js/api.js`:

```javascript
export async function createProject(payload) {
  const res = await fetch('/api/projects/create', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  return { status: res.status, body };
}
```

- [ ] **Step 2: Create the wizard module (state machine + navigation)**

Create `dashboard/static/js/project-wizard.js`:

```javascript
import { createProject } from './api.js';

const stepDefs = [
  { id: 'identity', title: 'Identity' },
  { id: 'jira',     title: 'Jira' },
  { id: 'vcs',      title: 'VCS' },
  { id: 'quality',  title: 'Quality' },
  { id: 'extras',   title: 'Extras' },
  { id: 'review',   title: 'Review' },
];

const state = {
  step: 0,
  data: {
    identity: {},
    jira: { statuses: { todo: 'To Do', in_progress: 'In Progress', in_review: 'In Review', done: 'Done' }, trigger_labels: [], ignore_labels: [] },
    vcs: { provider: 'github', github: { default_branch: 'develop', branch_prefix: 'feature', merge_method: 'squash' }, gitlab: {} },
    quality: { lint: {hard_gate: true}, test: {hard_gate: true}, build: {hard_gate: true} },
    extras: { protected_files: [] },
  },
  errors: {},
  running: null,
};

let els;

export function openWizard() {
  els = {
    modal: document.getElementById('project-wizard-modal'),
    steps: document.getElementById('wizard-steps'),
    body: document.getElementById('wizard-body'),
    back: document.getElementById('wizard-back'),
    next: document.getElementById('wizard-next'),
    close: document.getElementById('wizard-close'),
  };
  els.modal.classList.remove('hidden');
  els.back.onclick = onBack;
  els.next.onclick = onNext;
  els.close.onclick = closeWizard;
  state.step = 0;
  render();
}

function closeWizard() {
  els.modal.classList.add('hidden');
}

function render() {
  renderSteps();
  const def = stepDefs[state.step];
  const renderer = renderers[def.id];
  els.body.innerHTML = '';
  renderer(els.body);
  els.back.disabled = state.step === 0;
  els.next.textContent = state.step === stepDefs.length - 1 ? 'Create project' : 'Next';
}

function renderSteps() {
  els.steps.innerHTML = '';
  stepDefs.forEach((def, i) => {
    const el = document.createElement('span');
    el.className = 'step' + (i === state.step ? ' active' : i < state.step ? ' done' : '');
    el.textContent = `${i + 1}. ${def.title}`;
    els.steps.appendChild(el);
  });
}

function onBack() {
  if (state.step > 0) {
    state.step -= 1;
    render();
  }
}

async function onNext() {
  const def = stepDefs[state.step];
  const validator = validators[def.id];
  const errors = validator(state.data[def.id]);
  if (Object.keys(errors).length > 0) {
    state.errors = errors;
    render();
    return;
  }
  state.errors = {};
  if (state.step === stepDefs.length - 1) {
    await submit();
  } else {
    state.step += 1;
    render();
  }
}

// Step renderers and validators are populated in Tasks 17–22.
const renderers = {};
const validators = {};

async function submit() {
  const payload = buildPayload();
  els.body.innerHTML = '<div class="status-panel"><p>Submitting…</p></div>';
  els.back.disabled = true;
  els.next.disabled = true;
  const { status, body } = await createProject(payload);
  if (status !== 202) {
    renderSubmitError(status, body);
    els.next.disabled = false;
    return;
  }
  state.running = body;
  pollStatus();
}

function buildPayload() {
  // Assembled in Task 21 — this stub lets tests import the module.
  return state.data;
}

function renderSubmitError(status, body) {
  // Populated in Task 22.
  els.body.innerHTML = `<div class="status-panel failed"><p>Error ${status}: ${JSON.stringify(body)}</p></div>`;
}

function pollStatus() {
  // Populated in Task 21.
}

// Expose for Task 17+ module additions.
export const _internal = { state, stepDefs, renderers, validators };
```

- [ ] **Step 3: Verify the wizard opens and closes**

Start the dashboard and click "New Project". Expected: modal opens with six step chips; Back/Next buttons exist but step bodies are empty. Close button dismisses.

- [ ] **Step 4: Commit**

```bash
git add dashboard/static/js/api.js dashboard/static/js/project-wizard.js
git commit -m "feat(dashboard): wizard state machine and step framework"
```

---

## Task 17: Step 1 — Identity

**Files:**
- Modify: `dashboard/static/js/project-wizard.js`

- [ ] **Step 1: Add the identity renderer and validator**

In `project-wizard.js`, replace the empty `renderers = {}` and `validators = {}` lines with:

```javascript
const SLUG_RE = /^[a-z][a-z0-9-]{0,62}$/;

const renderers = {
  identity(body) {
    const d = state.data.identity;
    const e = state.errors;
    body.innerHTML = `
      <h3>Identity</h3>
      <div class="form-field">
        <label>Project ID (slug)</label>
        <input id="f-project-id" value="${d.project_id || ''}" placeholder="acme" />
        ${e.project_id ? `<span class="error">${e.project_id}</span>` : ''}
      </div>
      <div class="form-field">
        <label>Display name</label>
        <input id="f-display-name" value="${d.display_name || ''}" placeholder="Acme Corp" />
        ${e.display_name ? `<span class="error">${e.display_name}</span>` : ''}
      </div>
      <div class="form-field">
        <label>Repo ID (slug)</label>
        <input id="f-repo-id" value="${d.repo_id || ''}" placeholder="acme-app" />
        ${e.repo_id ? `<span class="error">${e.repo_id}</span>` : ''}
      </div>
      <div class="form-field">
        <label>Repo display name</label>
        <input id="f-repo-display-name" value="${d.repo_display_name || ''}" />
        ${e.repo_display_name ? `<span class="error">${e.repo_display_name}</span>` : ''}
      </div>
    `;
    body.querySelector('#f-project-id').oninput = (ev) => d.project_id = ev.target.value;
    body.querySelector('#f-display-name').oninput = (ev) => d.display_name = ev.target.value;
    body.querySelector('#f-repo-id').oninput = (ev) => d.repo_id = ev.target.value;
    body.querySelector('#f-repo-display-name').oninput = (ev) => d.repo_display_name = ev.target.value;
  },
};

const validators = {
  identity(d) {
    const errors = {};
    if (!d.project_id) errors.project_id = 'required';
    else if (!SLUG_RE.test(d.project_id)) errors.project_id = 'must be a lowercase slug';
    if (!d.display_name) errors.display_name = 'required';
    if (!d.repo_id) errors.repo_id = 'required';
    else if (!SLUG_RE.test(d.repo_id)) errors.repo_id = 'must be a lowercase slug';
    if (!d.repo_display_name) errors.repo_display_name = 'required';
    return errors;
  },
};
```

- [ ] **Step 2: Manual check**

Open the wizard, confirm Step 1 renders inputs, type a bad slug, click Next → error is shown. Fix → Next advances.

- [ ] **Step 3: Commit**

```bash
git add dashboard/static/js/project-wizard.js
git commit -m "feat(wizard): step 1 identity"
```

---

## Task 18: Step 2 — Jira (with chip input)

**Files:**
- Modify: `dashboard/static/js/project-wizard.js`

- [ ] **Step 1: Add a chip-input helper**

Add inside `project-wizard.js` above `const renderers`:

```javascript
function mountChipInput(container, values, onChange) {
  const wrap = document.createElement('div');
  wrap.className = 'chip-input';
  const renderChips = () => {
    wrap.innerHTML = '';
    values.forEach((v, idx) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.innerHTML = `${v}<button type="button">×</button>`;
      chip.querySelector('button').onclick = () => {
        values.splice(idx, 1);
        renderChips();
        onChange(values);
      };
      wrap.appendChild(chip);
    });
    const input = document.createElement('input');
    input.placeholder = 'Add label…';
    input.onkeydown = (ev) => {
      if ((ev.key === 'Enter' || ev.key === ',') && input.value.trim()) {
        ev.preventDefault();
        values.push(input.value.trim());
        onChange(values);
        renderChips();
      } else if (ev.key === 'Backspace' && !input.value && values.length) {
        values.pop();
        onChange(values);
        renderChips();
      }
    };
    wrap.appendChild(input);
    input.focus();
  };
  renderChips();
  container.appendChild(wrap);
}
```

- [ ] **Step 2: Add the Jira renderer and validator**

Inside the `renderers` object:

```javascript
  jira(body) {
    const d = state.data.jira;
    const e = state.errors;
    body.innerHTML = `
      <h3>Jira</h3>
      <div class="form-field"><label>URL</label><input id="f-jira-url" value="${d.url || ''}" placeholder="https://acme.atlassian.net" />${e.url ? `<span class="error">${e.url}</span>` : ''}</div>
      <div class="form-field"><label>Project key</label><input id="f-jira-key" value="${d.project_key || ''}" placeholder="ACME" />${e.project_key ? `<span class="error">${e.project_key}</span>` : ''}</div>
      <div class="form-field"><label>Email</label><input id="f-jira-email" value="${d.email || ''}" /></div>
      <div class="form-field"><label>API token</label><input type="password" id="f-jira-token" value="${d.token || ''}" />${e.token ? `<span class="error">${e.token}</span>` : ''}</div>
      <div class="form-field"><label>Trigger labels (all required on ticket)</label><div id="f-jira-labels"></div>${e.trigger_labels ? `<span class="error">${e.trigger_labels}</span>` : ''}</div>
      <div class="form-field"><label>Ignore labels</label><div id="f-jira-ignore"></div></div>
      <h4>Status mappings</h4>
      <div class="form-field"><label>To-Do</label><input id="f-jira-todo" value="${d.statuses.todo}" /></div>
      <div class="form-field"><label>In Progress</label><input id="f-jira-inprog" value="${d.statuses.in_progress}" /></div>
      <div class="form-field"><label>In Review</label><input id="f-jira-inrev" value="${d.statuses.in_review}" /></div>
      <div class="form-field"><label>Done</label><input id="f-jira-done" value="${d.statuses.done}" /></div>
    `;
    body.querySelector('#f-jira-url').oninput = (ev) => d.url = ev.target.value;
    body.querySelector('#f-jira-key').oninput = (ev) => d.project_key = ev.target.value;
    body.querySelector('#f-jira-email').oninput = (ev) => d.email = ev.target.value;
    body.querySelector('#f-jira-token').oninput = (ev) => d.token = ev.target.value;
    body.querySelector('#f-jira-todo').oninput = (ev) => d.statuses.todo = ev.target.value;
    body.querySelector('#f-jira-inprog').oninput = (ev) => d.statuses.in_progress = ev.target.value;
    body.querySelector('#f-jira-inrev').oninput = (ev) => d.statuses.in_review = ev.target.value;
    body.querySelector('#f-jira-done').oninput = (ev) => d.statuses.done = ev.target.value;
    mountChipInput(body.querySelector('#f-jira-labels'), d.trigger_labels, () => {});
    mountChipInput(body.querySelector('#f-jira-ignore'), d.ignore_labels, () => {});
  },
```

And add to `validators`:

```javascript
  jira(d) {
    const errors = {};
    if (!d.url) errors.url = 'required';
    if (!d.project_key) errors.project_key = 'required';
    if (!d.token) errors.token = 'required';
    if (!d.trigger_labels || d.trigger_labels.length === 0) errors.trigger_labels = 'at least one label required';
    return errors;
  },
```

- [ ] **Step 2: Manual check**

Open wizard, advance to step 2, add two chips, verify Next advances; clear chips, Next shows error.

- [ ] **Step 3: Commit**

```bash
git add dashboard/static/js/project-wizard.js
git commit -m "feat(wizard): step 2 jira with chip input"
```

---

## Task 19: Step 3 — VCS

**Files:**
- Modify: `dashboard/static/js/project-wizard.js`

- [ ] **Step 1: Add the VCS renderer and validator**

Inside `renderers`:

```javascript
  vcs(body) {
    const d = state.data.vcs;
    const e = state.errors;
    const common = `
      <div class="form-field">
        <label>Provider</label>
        <select id="f-vcs-provider">
          <option value="github" ${d.provider === 'github' ? 'selected' : ''}>GitHub</option>
          <option value="gitlab" ${d.provider === 'gitlab' ? 'selected' : ''}>GitLab</option>
        </select>
      </div>
    `;
    const github = `
      <div class="form-field"><label>Owner</label><input id="f-gh-owner" value="${d.github.owner || ''}" />${e.owner ? `<span class="error">${e.owner}</span>` : ''}</div>
      <div class="form-field"><label>Repo</label><input id="f-gh-repo" value="${d.github.repo || ''}" />${e.repo ? `<span class="error">${e.repo}</span>` : ''}</div>
      <div class="form-field"><label>Token</label><input type="password" id="f-gh-token" value="${d.github.token || ''}" />${e.token ? `<span class="error">${e.token}</span>` : ''}</div>
      <div class="form-field"><label>Default branch</label><input id="f-gh-branch" value="${d.github.default_branch}" /></div>
      <div class="form-field"><label>Branch prefix</label><input id="f-gh-prefix" value="${d.github.branch_prefix}" /></div>
      <div class="form-field"><label>Merge method</label>
        <select id="f-gh-merge">
          <option value="squash" ${d.github.merge_method === 'squash' ? 'selected' : ''}>squash</option>
          <option value="merge" ${d.github.merge_method === 'merge' ? 'selected' : ''}>merge</option>
          <option value="rebase" ${d.github.merge_method === 'rebase' ? 'selected' : ''}>rebase</option>
        </select>
      </div>
    `;
    const gitlab = `
      <div class="form-field"><label>GitLab URL</label><input id="f-gl-url" value="${d.gitlab.url || 'https://gitlab.com'}" /></div>
      <div class="form-field"><label>Project ID (numeric)</label><input id="f-gl-pid" value="${d.gitlab.project_id || ''}" /></div>
      <div class="form-field"><label>Token</label><input type="password" id="f-gl-token" value="${d.gitlab.token || ''}" /></div>
      <div class="form-field"><label>Default branch</label><input id="f-gl-branch" value="${d.gitlab.default_branch || 'develop'}" /></div>
      <div class="form-field"><label>Branch prefix</label><input id="f-gl-prefix" value="${d.gitlab.branch_prefix || 'feature'}" /></div>
    `;
    body.innerHTML = `<h3>VCS</h3>${common}<div id="vcs-provider-fields">${d.provider === 'github' ? github : gitlab}</div>`;
    const rerender = () => renderers.vcs(body);
    body.querySelector('#f-vcs-provider').onchange = (ev) => { d.provider = ev.target.value; rerender(); };
    if (d.provider === 'github') {
      body.querySelector('#f-gh-owner').oninput = (ev) => d.github.owner = ev.target.value;
      body.querySelector('#f-gh-repo').oninput = (ev) => d.github.repo = ev.target.value;
      body.querySelector('#f-gh-token').oninput = (ev) => d.github.token = ev.target.value;
      body.querySelector('#f-gh-branch').oninput = (ev) => d.github.default_branch = ev.target.value;
      body.querySelector('#f-gh-prefix').oninput = (ev) => d.github.branch_prefix = ev.target.value;
      body.querySelector('#f-gh-merge').onchange = (ev) => d.github.merge_method = ev.target.value;
    } else {
      body.querySelector('#f-gl-url').oninput = (ev) => d.gitlab.url = ev.target.value;
      body.querySelector('#f-gl-pid').oninput = (ev) => d.gitlab.project_id = ev.target.value;
      body.querySelector('#f-gl-token').oninput = (ev) => d.gitlab.token = ev.target.value;
      body.querySelector('#f-gl-branch').oninput = (ev) => d.gitlab.default_branch = ev.target.value;
      body.querySelector('#f-gl-prefix').oninput = (ev) => d.gitlab.branch_prefix = ev.target.value;
    }
  },
```

Inside `validators`:

```javascript
  vcs(d) {
    const errors = {};
    if (d.provider === 'github') {
      if (!d.github.owner) errors.owner = 'required';
      if (!d.github.repo) errors.repo = 'required';
      if (!d.github.token) errors.token = 'required';
    } else {
      if (!d.gitlab.url) errors.url = 'required';
      if (!d.gitlab.project_id) errors.project_id = 'required';
      if (!d.gitlab.token) errors.token = 'required';
    }
    return errors;
  },
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/static/js/project-wizard.js
git commit -m "feat(wizard): step 3 vcs with github/gitlab toggle"
```

---

## Task 20: Steps 4 & 5 — Quality and Extras

**Files:**
- Modify: `dashboard/static/js/project-wizard.js`

- [ ] **Step 1: Add the Quality and Extras renderers**

Inside `renderers`:

```javascript
  quality(body) {
    const d = state.data.quality;
    const row = (key, label) => `
      <div class="form-field">
        <label>${label} command</label>
        <input id="f-q-${key}-cmd" value="${d[key].command || ''}" placeholder="optional" />
        <label><input type="checkbox" id="f-q-${key}-gate" ${d[key].hard_gate ? 'checked' : ''}/> Hard gate</label>
      </div>
    `;
    body.innerHTML = `<h3>Quality gates</h3>${row('lint', 'Lint')}${row('test', 'Test')}${row('build', 'Build')}`;
    for (const key of ['lint', 'test', 'build']) {
      body.querySelector(`#f-q-${key}-cmd`).oninput = (ev) => d[key].command = ev.target.value;
      body.querySelector(`#f-q-${key}-gate`).onchange = (ev) => d[key].hard_gate = ev.target.checked;
    }
  },
  extras(body) {
    const d = state.data.extras;
    body.innerHTML = `
      <h3>Extras</h3>
      <div class="form-field"><label>Telegram bot token (optional)</label><input type="password" id="f-ex-tg-token" value="${d.telegram_bot_token || ''}" placeholder="blank = inherit global" /></div>
      <div class="form-field"><label>Telegram chat ID (optional)</label><input id="f-ex-tg-chat" value="${d.telegram_chat_id || ''}" /></div>
      <div class="form-field"><label>Architecture rules file</label><input id="f-ex-arch" value="${d.arch_rules_file || ''}" placeholder="docs/arch-rules.md" /></div>
      <div class="form-field"><label>Protected files (comma-separated)</label><input id="f-ex-protected" value="${(d.protected_files || []).join(', ')}" /></div>
      <div class="form-field"><label>Max concurrent tickets (optional)</label><input id="f-ex-max" type="number" value="${d.max_concurrent_tickets || ''}" /></div>
    `;
    body.querySelector('#f-ex-tg-token').oninput = (ev) => d.telegram_bot_token = ev.target.value || null;
    body.querySelector('#f-ex-tg-chat').oninput = (ev) => d.telegram_chat_id = ev.target.value || null;
    body.querySelector('#f-ex-arch').oninput = (ev) => d.arch_rules_file = ev.target.value || null;
    body.querySelector('#f-ex-protected').oninput = (ev) => d.protected_files = ev.target.value.split(',').map(s => s.trim()).filter(Boolean);
    body.querySelector('#f-ex-max').oninput = (ev) => d.max_concurrent_tickets = ev.target.value ? parseInt(ev.target.value, 10) : null;
  },
```

Inside `validators` (both trivial — no hard errors):

```javascript
  quality() { return {}; },
  extras() { return {}; },
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/static/js/project-wizard.js
git commit -m "feat(wizard): steps 4 quality and 5 extras"
```

---

## Task 21: Step 6 — Review, submit, and live status

**Files:**
- Modify: `dashboard/static/js/project-wizard.js`

- [ ] **Step 1: Add the Review renderer**

Inside `renderers`:

```javascript
  review(body) {
    const d = state.data;
    const prefix = (d.identity.project_id || '').toUpperCase().replace(/-/g, '_');
    const tokenHint = (name) => `<code>${prefix}_${name}</code>`;
    const vcsProvider = d.vcs.provider;
    const vcsBlock = vcsProvider === 'github'
      ? `<li>Owner/repo: ${d.vcs.github.owner}/${d.vcs.github.repo}</li>
         <li>Token: •••• → will be saved to ${tokenHint('GITHUB_TOKEN')}</li>`
      : `<li>URL: ${d.vcs.gitlab.url}</li>
         <li>Project ID: ${d.vcs.gitlab.project_id}</li>
         <li>Token: •••• → will be saved to ${tokenHint('GITLAB_TOKEN')}</li>`;
    body.innerHTML = `
      <h3>Review</h3>
      <h4>Identity</h4>
      <ul>
        <li>project_id: ${d.identity.project_id}</li>
        <li>display_name: ${d.identity.display_name}</li>
        <li>repo_id: ${d.identity.repo_id}</li>
      </ul>
      <h4>Jira</h4>
      <ul>
        <li>URL: ${d.jira.url}</li>
        <li>Project key: ${d.jira.project_key}</li>
        <li>Trigger labels: ${d.jira.trigger_labels.join(', ')}</li>
        <li>Token: •••• → will be saved to ${tokenHint('JIRA_TOKEN')}</li>
      </ul>
      <h4>VCS (${vcsProvider})</h4>
      <ul>${vcsBlock}</ul>
      <h4>Quality</h4>
      <ul>
        <li>lint: ${d.quality.lint.command || '—'}</li>
        <li>test: ${d.quality.test.command || '—'}</li>
        <li>build: ${d.quality.build.command || '—'}</li>
      </ul>
    `;
  },
```

Inside `validators`:

```javascript
  review() { return {}; },
```

- [ ] **Step 2: Implement `buildPayload`**

Replace the stub `buildPayload` with:

```javascript
function buildPayload() {
  const d = state.data;
  const vcs = { provider: d.vcs.provider };
  if (d.vcs.provider === 'github') vcs.github = { ...d.vcs.github };
  else vcs.gitlab = { ...d.vcs.gitlab };
  return {
    identity: { ...d.identity },
    jira: { ...d.jira, trigger_labels: [...d.jira.trigger_labels], ignore_labels: [...d.jira.ignore_labels] },
    vcs,
    quality: { ...d.quality },
    extras: { ...d.extras, protected_files: [...d.extras.protected_files] },
  };
}
```

- [ ] **Step 3: Implement `pollStatus`**

Replace the stub with:

```javascript
async function pollStatus() {
  const { workspace } = state.running;
  els.body.innerHTML = `
    <div class="status-panel">
      <h3>Setting up…</h3>
      <div>
        <span class="status-step" id="st-validating">VALIDATING</span>
        <span class="status-step" id="st-writing">WRITING</span>
        <span class="status-step" id="st-done">DONE</span>
      </div>
      <p id="st-message"></p>
    </div>
  `;
  const project = workspace.split('/')[0];
  let poll;
  const tick = async () => {
    const res = await fetch(`/api/workspaces?project_id=${project}`);
    const body = await res.json();
    const entry = (body.workspaces || []).find(
      (w) => w.workspace_root && w.workspace_root.endsWith('/setup')
        && w.company_id === project,
    );
    if (!entry) return;
    const st = entry.current_state;
    const active = (id, on) => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle('active', on);
    };
    const done = (id, on) => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle('done', on);
    };
    if (st === 'VALIDATING') { active('st-validating', true); }
    if (st === 'WRITING') { done('st-validating', true); active('st-writing', true); }
    if (st === 'SETUP_DONE') {
      done('st-validating', true); done('st-writing', true); done('st-done', true);
      clearInterval(poll);
      setTimeout(closeWizard, 3000);
      window.dispatchEvent(new CustomEvent('cleave:projects-changed'));
    }
    if (st === 'SETUP_FAILED') {
      clearInterval(poll);
      await renderFailure(entry);
    }
  };
  poll = setInterval(tick, 2000);
  tick();
}
```

- [ ] **Step 4: Commit**

```bash
git add dashboard/static/js/project-wizard.js
git commit -m "feat(wizard): step 6 review, submit, and live status polling"
```

---

## Task 22: Failure rendering and retry

**Files:**
- Modify: `dashboard/static/js/project-wizard.js`

- [ ] **Step 1: Implement `renderFailure` and `renderSubmitError`**

Append (or replace the stub) in `project-wizard.js`:

```javascript
async function renderFailure(entry) {
  let report = '';
  try {
    const res = await fetch(`/api/workspaces/${encodeURIComponent(entry.ticket_id)}/report/project-setup-output.md`);
    report = await res.text();
  } catch {}
  els.body.innerHTML = `
    <div class="status-panel failed">
      <h3>Setup failed</h3>
      <pre style="text-align:left;white-space:pre-wrap;max-height:300px;overflow:auto;">${escapeHtml(report)}</pre>
      <button id="retry-btn" class="btn-primary">Edit & retry</button>
    </div>
  `;
  document.getElementById('retry-btn').onclick = () => {
    // Clear secrets, keep other values
    state.data.jira.token = '';
    if (state.data.vcs.provider === 'github') state.data.vcs.github.token = '';
    else state.data.vcs.gitlab.token = '';
    state.data.extras.telegram_bot_token = null;
    state.running = null;
    state.step = 0;
    els.next.disabled = false;
    render();
  };
}

function renderSubmitError(status, body) {
  els.body.innerHTML = `
    <div class="status-panel failed">
      <h3>Error ${status}</h3>
      <pre>${escapeHtml(JSON.stringify(body, null, 2))}</pre>
      <button id="back-to-form" class="btn-primary">Back to form</button>
    </div>
  `;
  document.getElementById('back-to-form').onclick = () => {
    state.step = 0;
    els.next.disabled = false;
    render();
  };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/static/js/project-wizard.js
git commit -m "feat(wizard): failure rendering and retry flow"
```

---

# Phase 7 — Atlas production wiring

## Task 23: Wire a real `atlas_fn` into `main.py`

**Files:**
- Modify: `main.py`

The integration test uses a fake Atlas. The production app needs the real one — the existing Atlas agent invoked via the orchestrator's agent-runner. The exact API depends on current wiring; this task documents the shape of the required integration.

- [ ] **Step 1: Find how other agents are currently invoked**

Run: `grep -rn "run_agent\|AgentRunner\|invoke_agent" orchestrator/ main.py`

Expected: locate the function/class used elsewhere (e.g. `orchestrator.agent_runner.run_agent(workspace_dir, agent_name)`).

- [ ] **Step 2: Implement a production `atlas_fn`**

In `main.py`, near where `create_app` is called, add:

```python
async def _production_atlas_fn(workspace, config_dir):
    """Run the project-setup-agent against the setup workspace.

    The agent reads meta/input.md in orchestrator mode, validates
    credentials using the existing sandboxed validators, and writes
    config YAMLs under config_dir.
    """
    from orchestrator.agent_runner import run_agent  # adjust to real path
    await run_agent(
        agent_name="project-setup-agent",
        workspace_dir=workspace.setup_dir,
        config_dir=config_dir,
        mode="orchestrator",
    )
```

Then pass this via the `global_config` the web app reads, e.g. attach as `global_config.atlas_fn = _production_atlas_fn` before `create_app(...)`.

(If `global_config` is a dataclass without that field, extend it with an optional attribute or pass `atlas_fn` directly as a new keyword arg to `create_app` — in that case also update `create_app`'s signature in `dashboard/web.py` to accept `atlas_fn` and use it instead of `getattr(global_config, "atlas_fn", None)`.)

- [ ] **Step 3: Manual smoke test**

- Start the dashboard: `./run.sh`
- Browse to the dashboard, click "+ New Project"
- Fill in a throwaway payload pointing at a test project
- Submit and watch the status panel transition VALIDATING → WRITING → DONE
- Verify `config-live/projects/{id}/project.yaml` exists
- Verify `/data/cleave/{id}/{repo}/tickets/` exists
- Verify `.env` contains the new var names

- [ ] **Step 4: Commit**

```bash
git add main.py dashboard/web.py
git commit -m "feat(main): wire production atlas_fn for project-create endpoint"
```

---

# Phase 8 — Full suite verification

## Task 24: Run the full test suite

- [ ] **Step 1: Run everything**

Run: `pytest tests/ -v`
Expected: all pass. Fix any flakes or accidentally-broken tests before proceeding.

- [ ] **Step 2: Run lint/format checks**

Run: `ruff check .` and `ruff format --check .` (or whichever linters the project uses — check `pyproject.toml`).
Expected: clean. Fix any issues.

- [ ] **Step 3: Final commit**

No code changes expected. If any linting fixes were needed, commit them:

```bash
git add -A
git commit -m "chore: lint fixes after project-create wizard"
```

---

## Summary of commits (expected)

1. `refactor(config): rename JiraConfig.trigger_label to plural trigger_labels`
2. `feat(config): reject legacy singular trigger_label with migration error`
3. `feat(prioritizer): require all trigger_labels (AND semantics)`
4. `feat(jira): AND-filter tickets across multiple trigger_labels`
5. `chore(config): migrate acme and fixtures to trigger_labels plural`
6. `docs: update trigger_label → trigger_labels across docs and agent prompts`
7. `feat(dashboard): add atomic .env writer with collision detection`
8. `feat(dashboard): add setup workspace creator with state transitions`
9. `feat(dashboard): add project-create payload validation and redaction`
10. `feat(agent): add Atlas orchestrator-mode flow for dashboard handoff`
11. `feat(dashboard): add supervised Atlas runner for project-create`
12. `feat(dashboard): add POST /api/projects/create handler`
13. `feat(dashboard): register POST /api/projects/create and tag setup workspaces`
14. `test(integration): end-to-end project-create flow with rollback`
15. `feat(dashboard): add new-project button and wizard modal shell`
16. `feat(dashboard): wizard state machine and step framework`
17. `feat(wizard): step 1 identity`
18. `feat(wizard): step 2 jira with chip input`
19. `feat(wizard): step 3 vcs with github/gitlab toggle`
20. `feat(wizard): steps 4 quality and 5 extras`
21. `feat(wizard): step 6 review, submit, and live status polling`
22. `feat(wizard): failure rendering and retry flow`
23. `feat(main): wire production atlas_fn for project-create endpoint`
24. (optional) `chore: lint fixes after project-create wizard`
