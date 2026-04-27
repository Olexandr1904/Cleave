import asyncio
import json
from pathlib import Path

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
    assert state["previous_state"] in ("WRITING", "VALIDATING")

    assert (tmp_path / "config-live" / "projects" / "acme" / "project.yaml").exists()
    env_content = (tmp_path / ".env").read_text()
    assert "ACME_JIRA_TOKEN=jira-raw" in env_content
    assert "ACME_GITHUB_TOKEN=gh-raw" in env_content
    input_md = (workspace_dir / "meta" / "input.md").read_text()
    assert "jira-raw" not in input_md
    assert "ACME_JIRA_TOKEN" in input_md
    assert (tmp_path / "workspace" / "acme" / "acme-app" / "tickets").is_dir()

    # After completion, the busy flag must be cleared so subsequent requests work.
    from dashboard import project_create as pc
    assert pc._busy is False
    assert pc._active_workspace is None


def test_atlas_failure_rolls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("ACME_JIRA_TOKEN", raising=False)

    async def failing_atlas(workspace, config_dir):
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

    assert not (tmp_path / "config-live" / "projects" / "acme").exists()
    env_content = (tmp_path / ".env").read_text() if (tmp_path / ".env").exists() else ""
    assert "ACME_JIRA_TOKEN" not in env_content
    import os
    assert "ACME_JIRA_TOKEN" not in os.environ
    report = workspace_dir / "reports" / "project-setup-output.md"
    assert "simulated validation failure" in report.read_text()

    from dashboard import project_create as pc
    assert pc._busy is False
    assert pc._active_workspace is None


def test_project_already_exists_returns_409(tmp_path):
    import asyncio
    (tmp_path / "config-live" / "projects" / "acme").mkdir(parents=True)
    app = _make_app(tmp_path, lambda w, c: asyncio.sleep(0))
    client = TestClient(app)
    resp = client.post("/api/projects/create", json=VALID_PAYLOAD)
    assert resp.status_code == 409
    assert resp.json()["error"] == "project_exists"


def test_env_var_conflict_returns_409(tmp_path):
    import asyncio
    (tmp_path / ".env").write_text("ACME_JIRA_TOKEN=old\n")
    app = _make_app(tmp_path, lambda w, c: asyncio.sleep(0))
    client = TestClient(app)
    resp = client.post("/api/projects/create", json=VALID_PAYLOAD)
    assert resp.status_code == 409
    assert resp.json()["error"] == "env_var_conflict"
    assert "ACME_JIRA_TOKEN" in resp.json()["vars"]


def test_bad_payload_returns_400(tmp_path):
    import asyncio
    app = _make_app(tmp_path, lambda w, c: asyncio.sleep(0))
    client = TestClient(app)
    bad = {**VALID_PAYLOAD, "jira": {**VALID_PAYLOAD["jira"], "trigger_labels": []}}
    resp = client.post("/api/projects/create", json=bad)
    assert resp.status_code == 400
    assert "jira.trigger_labels" in resp.json()["fields"]


def test_busy_flag_returns_429(tmp_path):
    """When another setup is in progress, the endpoint must return 429 without side effects."""
    import asyncio
    from dashboard import project_create as pc

    pc._busy = True
    pc._active_workspace = "other/other-app/setup"
    try:
        app = _make_app(tmp_path, lambda w, c: asyncio.sleep(0))
        client = TestClient(app)
        resp = client.post("/api/projects/create", json=VALID_PAYLOAD)
        assert resp.status_code == 429
        assert resp.json()["error"] == "busy"
        assert resp.json()["active_workspace"] == "other/other-app/setup"
        # No side effects: no env file created, no config dir
        assert not (tmp_path / ".env").exists()
        assert not (tmp_path / "config-live" / "projects" / "acme").exists()
    finally:
        pc._busy = False
        pc._active_workspace = None


# ---------------------------------------------------------------------------
# Helpers for the end-to-end integration test
# ---------------------------------------------------------------------------

_MINIMAL_GLOBAL_YAML_INT = """\
telegram:
  bot_token: ''
  default_chat_id: ''
claude:
  api_key: ''
workspaces:
  base_dir: {ws}
  max_age_days: 14
  min_free_disk_gb: 0
  max_workspace_size_gb: 2
defaults:
  poll_interval_seconds: 300
  max_iterations: {{scope_guard: 1, fix: 1, qa: 1, dev: 1}}
  max_parallel_tickets: 1
  pr_comment_fetch_delay_minutes: 30
logging:
  level: WARNING
  dir: {log}
heartbeat:
  enabled: false
  interval_hours: 24
operator:
  role: ''
  stack: []
  preferences:
    code_style: ''
  rules: []
"""

_STUB_PROJECT_YAML = """\
project:
  id: "{pid}"
  name: "Demo"
  enabled: true
jira:
  url: "https://example.atlassian.net"
  token: "stub"
  email: "t@t"
  project_key: "DEMO"
  trigger_labels: ["ai-pipeline"]
  ignore_labels: []
  statuses:
    todo: "To Do"
    in_progress: "In Progress"
    in_review: "In Review"
    done: "Done"
telegram:
  bot_token: ""
  default_chat_id: ""
parallelism:
  max_concurrent_tickets: 1
defaults:
  poll_interval_seconds: 300
  max_iterations:
    scope_guard: 1
    fix: 1
    qa: 1
    dev: 1
  pr_comment_fetch_delay_minutes: 30
"""

_STUB_REPO_YAML = """\
repo:
  id: "{rid}"
  name: "Demo Repo"
  enabled: true
vcs:
  provider: "github"
  github:
    token: "stub"
    owner: "demo-org"
    repo: "demo-repo"
"""


def _make_valid_payload(project_id: str, repo_id: str) -> dict:
    return {
        "identity": {
            "project_id": project_id,
            "display_name": "Demo",
            "repo_id": repo_id,
            "repo_display_name": "Demo Repo",
        },
        "jira": {
            "url": "https://example.atlassian.net",
            "project_key": "DEMO",
            "email": "t@t",
            "token": "stub",
            "trigger_labels": ["ai-pipeline"],
        },
        "vcs": {
            "provider": "github",
            "github": {"token": "stub", "owner": "demo-org", "repo": "demo-repo"},
        },
    }


# ---------------------------------------------------------------------------
# End-to-end test
# ---------------------------------------------------------------------------

import pytest


@pytest.mark.asyncio
async def test_wizard_creates_project_and_rescan_makes_it_live(tmp_path):
    """End-to-end: wizard POST → Atlas stub writes YAML → rescan adds project."""
    from unittest.mock import MagicMock

    from dashboard.event_store import EventStore
    from dashboard.events import EventBus
    from dashboard.web import create_app

    cfg_dir = tmp_path / "config-live"
    (cfg_dir / "projects").mkdir(parents=True)
    (cfg_dir / "global.yaml").write_text(
        _MINIMAL_GLOBAL_YAML_INT.format(
            ws=tmp_path / "ws",
            log=tmp_path / "log",
            db=tmp_path / "events.db",
        ),
        encoding="utf-8",
    )
    ws_base = tmp_path / "ws"
    ws_base.mkdir()

    projects_dict: dict = {}
    rescan_calls: list = []

    async def fake_rescan():
        rescan_calls.append(True)
        # Mimic real rescan: load_config → diff → merge.
        from config.config_loader import load_config
        try:
            _, loaded = load_config(str(cfg_dir))
        except Exception:
            return []
        added = []
        for pid, proj in loaded.items():
            if pid not in projects_dict:
                projects_dict[pid] = proj
                added.append(pid)
        return added

    orchestrator = MagicMock()
    orchestrator.rescan_projects = fake_rescan

    async def stub_atlas(setup_ws, cfg):
        pid = setup_ws.project_id
        rid = setup_ws.repo_id
        pdir = cfg / "projects" / pid
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "project.yaml").write_text(
            _STUB_PROJECT_YAML.format(pid=pid), encoding="utf-8",
        )
        (pdir / "repos").mkdir(exist_ok=True)
        (pdir / "repos" / f"{rid}.yaml").write_text(
            _STUB_REPO_YAML.format(rid=rid), encoding="utf-8",
        )

    bus = EventBus()
    store = EventStore(str(tmp_path / "events.db"))
    await store.initialize()

    app = create_app(
        bus, store,
        workspace_base_dir=str(ws_base),
        orchestrator=orchestrator,
        projects=projects_dict,
        config_dir=str(cfg_dir),
        atlas_fn=stub_atlas,
        env_path=str(tmp_path / ".env"),
    )

    with TestClient(app) as client:
        resp = client.post("/api/projects/create", json=_make_valid_payload("demo", "main"))
    assert resp.status_code == 202, resp.text

    for _ in range(40):
        if rescan_calls and "demo" in projects_dict:
            break
        await asyncio.sleep(0.05)

    assert rescan_calls, "orchestrator.rescan_projects was not called"
    assert "demo" in projects_dict, "demo project not merged into projects dict"
