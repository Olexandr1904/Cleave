"""E2E test fixtures — dashboard server + seeded workspaces + Playwright."""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.constants import RUNTIME_OUTPUT_BA

import pytest
import uvicorn

from dashboard.event_store import EventStore
from dashboard.events import Event, EventBus
from dashboard.web import create_app
from workspace.workspace import Stage, Workspace, WorkspaceState


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed_workspace(
    base_dir: Path,
    ticket_id: str,
    state: str,
    *,
    company: str = "acme",
    repo: str = "acme-app",
    previous_state: str | None = None,
    reports: list[str] | None = None,
    meta: list[str] | None = None,
    error: str | None = None,
    pr_url: str | None = None,
    pr_number: int | None = None,
) -> Path:
    """Create a workspace directory on disk with state.json + reports/meta."""
    ws_root = base_dir / company / repo / "tickets" / ticket_id
    ws_root.mkdir(parents=True, exist_ok=True)
    (ws_root / "reports").mkdir(exist_ok=True)
    (ws_root / "meta").mkdir(exist_ok=True)
    (ws_root / "logs").mkdir(exist_ok=True)
    (ws_root / "source").mkdir(exist_ok=True)

    now = _now_iso()
    state_obj = WorkspaceState(
        ticket_id=ticket_id,
        company_id=company,
        repo_id=repo,
        workspace_root=str(ws_root),
        branch=f"feature/{ticket_id.lower()}",
        current_state=state,
        previous_state=previous_state,
        started_at=now,
        last_updated_at=now,
        human_input_pending=state in ("BLOCKED", "AWAITING_APPROVAL", "MANUAL_CONTROL"),
        error=error,
        pr_url=pr_url,
        pr_number=pr_number,
    )
    (ws_root / "state.json").write_text(json.dumps(asdict(state_obj), indent=2))

    for r in reports or []:
        (ws_root / "reports" / r).write_text(f"# {r}\n\nTest report content for {ticket_id}.\n")
    for m in meta or []:
        (ws_root / "meta" / m).write_text(f"# {m}\n\nTest meta content for {ticket_id}.\n")

    return ws_root


class FakeAgentRuntime:
    """Minimal agent runtime stub — matches the surface used by take_control."""

    def __init__(self) -> None:
        self._running: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []

    def register_running(self, ticket_id: str, agent_id: str) -> None:
        self._running[ticket_id] = {
            "agent_id": agent_id,
            "started_at": time.time(),
            "pid": 0,
        }

    def get_running(self, ticket_id: str) -> dict[str, Any] | None:
        return self._running.get(ticket_id)

    def cancel(self, ticket_id: str) -> None:
        self.cancelled.append(ticket_id)
        self._running.pop(ticket_id, None)


class FakeOrchestrator:
    """Minimal orchestrator stub — exposes only what dashboard.actions needs."""

    def __init__(self, workspace_base_dir: Path) -> None:
        self._base = workspace_base_dir
        self._agent_runtime = FakeAgentRuntime()

    def get_active_workspaces(self) -> list[Workspace]:
        """Load all workspaces from disk as Workspace objects."""
        result = []
        for state_file in self._base.rglob("state.json"):
            ws = Workspace(str(state_file.parent))
            result.append(ws)
        return result


class FakeModeHandler:
    """Minimal mode handler stub."""

    VALID_MODES = {"auto", "manual", "paused"}

    def __init__(self) -> None:
        self._mode = "manual"

    def get_mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in self.VALID_MODES:
            raise ValueError(f"Invalid mode: {mode}")
        self._mode = mode


class FakeDashboardConfig:
    # "true" is a harmless Unix no-op — fulfills terminal_command contract
    # without spawning an actual terminal window during tests.
    terminal_command = "true"


def make_fake_projects(
    company_id: str = "acme",
    repo_id: str = "acme-app",
    jira_url: str = "https://acme.atlassian.net",
    github_owner: str = "acme",
    github_repo: str = "acme-app",
) -> dict[str, Any]:
    """Build a minimal LoadedProject-shaped dict for dashboard link tests."""
    from types import SimpleNamespace
    jira = SimpleNamespace(url=jira_url, project_key="ACME")
    github = SimpleNamespace(owner=github_owner, repo=github_repo)
    gitlab = SimpleNamespace(url="", project_id="")
    vcs = SimpleNamespace(provider="github", github=github, gitlab=gitlab)
    repo = SimpleNamespace(vcs=vcs)
    config = SimpleNamespace(jira=jira)
    project = SimpleNamespace(config=config, repos={repo_id: repo})
    return {company_id: project}


class FakeGlobalConfig:
    dashboard = FakeDashboardConfig()


@pytest.fixture
def seeded_workspaces(tmp_path: Path) -> Path:
    """Default seed: 3 workspaces covering common states.
    Tests that need different seeds should use `workspace_seeder` instead.
    """
    base = tmp_path / "sickle"
    base.mkdir()

    _seed_workspace(
        base, "SPIKE-1", "DEV",
        reports=[RUNTIME_OUTPUT_BA],
        meta=["ticket.md"],
    )
    _seed_workspace(
        base, "SPIKE-2", "AWAITING_APPROVAL",
        previous_state="ANALYSIS",
        reports=[RUNTIME_OUTPUT_BA],
        meta=["ticket.md"],
    )
    _seed_workspace(
        base, "SPIKE-3", "BLOCKED",
        previous_state="DEV",
        error="Test blocker: needs human input",
        meta=["ticket.md"],
    )
    return base


@pytest.fixture
def workspace_seeder(tmp_path: Path):
    """Returns (base_dir, seed_fn) for tests that need custom workspace setups."""
    base = tmp_path / "sickle"
    base.mkdir()

    def seed(ticket_id: str, state: str, **kwargs) -> Path:
        return _seed_workspace(base, ticket_id, state, **kwargs)

    return base, seed


def _start_server(
    workspace_base_dir: Path,
    db_path: str,
    seed_events: list[Event] | None = None,
    projects: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start uvicorn in a background thread. Returns server control dict."""
    import asyncio

    port = _free_port()
    bus = EventBus()
    base_url = f"http://127.0.0.1:{port}"
    mode_handler = FakeModeHandler()
    orchestrator = FakeOrchestrator(workspace_base_dir)

    server_holder: dict[str, Any] = {}
    ready_event = threading.Event()
    error_holder: dict[str, Any] = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            store = EventStore(db_path)
            await store.initialize()
            for ev in seed_events or []:
                await store.insert(ev)
            server_holder["store"] = store

            app = create_app(
                bus=bus,
                store=store,
                workspace_base_dir=str(workspace_base_dir),
                orchestrator=orchestrator,
                mode_handler=mode_handler,
                global_config=FakeGlobalConfig(),
                projects=projects,
            )
            config = uvicorn.Config(
                app=app, host="127.0.0.1", port=port,
                log_level="warning", loop="asyncio",
            )
            server = uvicorn.Server(config)
            server_holder["server"] = server
            ready_event.set()
            await server.serve()
            await store.close()

        try:
            loop.run_until_complete(main())
        except Exception as e:
            error_holder["error"] = e
            ready_event.set()
        finally:
            loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready_event.wait(timeout=5)
    if "error" in error_holder:
        raise error_holder["error"]

    # Wait for HTTP readiness
    import urllib.request
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("Dashboard server did not respond to /api/health")

    return {
        "base_url": base_url,
        "workspace_dir": workspace_base_dir,
        "event_bus": bus,
        "mode_handler": mode_handler,
        "orchestrator": orchestrator,
        "server": server_holder.get("server"),
        "thread": thread,
    }


def _stop_server(ctx: dict[str, Any]) -> None:
    server = ctx.get("server")
    if server is not None:
        server.should_exit = True
    thread = ctx.get("thread")
    if thread is not None:
        thread.join(timeout=5)


@pytest.fixture
def dashboard_server(seeded_workspaces: Path, tmp_path: Path):
    """Server backed by the default 3-workspace seed."""
    ctx = _start_server(seeded_workspaces, str(tmp_path / "events.db"))
    yield ctx
    _stop_server(ctx)


@pytest.fixture
def dashboard_server_custom(workspace_seeder, tmp_path: Path):
    """Server over an empty workspace dir — test seeds what it needs, then starts.

    Usage:
        base, seed = workspace_seeder
        seed("T-1", "DEV")
        seed("T-2", "BLOCKED", previous_state="DEV")
        ctx = dashboard_server_custom()
    """
    base, _seed = workspace_seeder
    contexts: list[dict[str, Any]] = []

    def start(events: list[Event] | None = None, projects=None):
        ctx = _start_server(
            base, str(tmp_path / "events.db"),
            seed_events=events, projects=projects,
        )
        contexts.append(ctx)
        return ctx

    yield start

    for ctx in contexts:
        _stop_server(ctx)


def goto_and_wait_for_board(page, base_url: str) -> None:
    """Open the dashboard and wait for the board to render at least one card."""
    page.goto(base_url)
    page.wait_for_selector(".card[data-ticket]", timeout=5000)


def wait_for_state_change(state_path: Path, expected_not: str, timeout: float = 3.0) -> dict:
    """Poll state.json until current_state != expected_not, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = json.loads(state_path.read_text())
            if data.get("current_state") != expected_not:
                return data
        except Exception:
            pass
        time.sleep(0.05)
    raise AssertionError(
        f"State did not change from {expected_not} within {timeout}s "
        f"(last: {state_path.read_text() if state_path.exists() else 'missing'})"
    )
