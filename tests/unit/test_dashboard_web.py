from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dashboard import web
from dashboard.event_store import EventStore
from dashboard.events import Event, EventBus
from dashboard.web import create_app, _dir_size_bytes, _scan_all_workspaces


class TestDirSizeBytes:
    def test_sums_nested_file_sizes(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"x" * 100)
        sub = tmp_path / "sub" / "deep"
        sub.mkdir(parents=True)
        (sub / "b.bin").write_bytes(b"y" * 250)
        assert _dir_size_bytes(tmp_path) == 350

    def test_missing_dir_returns_zero(self, tmp_path):
        assert _dir_size_bytes(tmp_path / "nope") == 0

    def test_does_not_follow_symlinks(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "f").write_bytes(b"z" * 500)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        # The 500-byte file is counted once (via real/), not again via the symlink.
        assert _dir_size_bytes(tmp_path) == 500


class TestScanIncludesDiskBytes:
    def _write_state(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        (root / "state.json").write_text(json.dumps({
            "ticket_id": "T-1", "company_id": "acme", "repo_id": "acme-app",
            "current_state": "QA", "title": "t",
        }), encoding="utf-8")

    def test_card_dict_has_disk_bytes(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-1"
        self._write_state(ws)
        (ws / "source").mkdir()
        (ws / "source" / "big.bin").write_bytes(b"q" * 1234)

        _scan_all_workspaces(str(tmp_path))
        web._disk_size_pool.submit(lambda: None).result(5)  # the one worker has measured the workspace
        results = _scan_all_workspaces(str(tmp_path))

        assert results[0]["disk_bytes"] >= 1234
        assert isinstance(results[0]["disk_bytes"], int)

    def test_scan_does_not_wait_for_the_disk_size_walk(self, tmp_path, monkeypatch):
        """Summing a multi-GB checkout takes seconds, and the board's scan runs on the daemon's event loop: the
        card shows the last measured size (none before the first) while a background thread measures."""
        self._write_state(tmp_path / "acme" / "acme-app" / "tickets" / "T-1")
        release = threading.Event()

        def slow_size(root):
            release.wait(2)
            return 4321

        monkeypatch.setattr(web, "_dir_size_bytes", slow_size)
        started = time.monotonic()
        assert _scan_all_workspaces(str(tmp_path))[0]["disk_bytes"] is None
        assert time.monotonic() - started < 1
        release.set()
        web._disk_size_pool.submit(lambda: None).result(5)
        assert _scan_all_workspaces(str(tmp_path))[0]["disk_bytes"] == 4321


def test_scan_reads_the_workspace_layout_not_the_checkouts(tmp_path):
    """The board scans on every load: an rglob walked every file of every checkout (10 s for 21 GB). Workspaces sit
    at <project>/<repo>/tickets/<id>/ and <project>/<repo>/setup/; a state.json inside a checkout is not one."""
    for root, ticket_id in ((tmp_path / "acme" / "acme-app" / "tickets" / "T-1", "T-1"),
                            (tmp_path / "acme" / "acme-app" / "setup", "setup")):
        root.mkdir(parents=True)
        (root / "state.json").write_text(json.dumps({"ticket_id": ticket_id, "company_id": "acme", "title": "t"}))
    stray = tmp_path / "acme" / "acme-app" / "tickets" / "T-1" / "source" / "fixtures" / "state.json"
    stray.parent.mkdir(parents=True)
    stray.write_text(json.dumps({"ticket_id": "FIXTURE", "title": "t"}))

    assert sorted(w["ticket_id"] for w in _scan_all_workspaces(str(tmp_path))) == ["T-1", "setup"]


@pytest.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = EventStore(db_path)
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def client(bus, store):
    app = create_app(bus, store)
    return TestClient(app)


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestEventsEndpoint:
    async def test_get_events_empty(self, client, store):
        resp = client.get("/api/events")
        assert resp.status_code == 200
        assert resp.json()["events"] == []

    async def test_get_events_with_data(self, client, store):
        await store.insert(Event(event_type="test", message="hello"))
        resp = client.get("/api/events")
        data = resp.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["message"] == "hello"

    async def test_get_events_with_limit(self, client, store):
        for i in range(10):
            await store.insert(Event(event_type="test", message=f"e{i}"))
        resp = client.get("/api/events?limit=3")
        assert len(resp.json()["events"]) == 3

    async def test_get_events_filtered_by_project(self, client, store):
        await store.insert(Event(event_type="a", message="p1", project_id="proj1"))
        await store.insert(Event(event_type="b", message="p2", project_id="proj2"))
        resp = client.get("/api/events?project_id=proj1")
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["project_id"] == "proj1"

    async def test_get_events_filtered_by_ticket(self, client, store):
        await store.insert(Event(event_type="a", message="t1", ticket_id="T-1"))
        await store.insert(Event(event_type="b", message="t2", ticket_id="T-2"))
        resp = client.get("/api/events?ticket_id=T-1")
        events = resp.json()["events"]
        assert len(events) == 1


class TestProjectsEndpoint:
    async def test_get_projects(self, client, store):
        await store.insert(Event(event_type="a", message="x", project_id="p1"))
        await store.insert(Event(event_type="b", message="y", project_id="p2"))
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        projects = resp.json()["projects"]
        assert "p1" in projects
        assert "p2" in projects


class TestTicketsEndpoint:
    async def test_get_tickets_for_project(self, client, store):
        await store.insert(Event(
            event_type="a", message="x", project_id="p1", ticket_id="T-1",
        ))
        await store.insert(Event(
            event_type="b", message="y", project_id="p1", ticket_id="T-2",
        ))
        resp = client.get("/api/projects/p1/tickets")
        assert resp.status_code == 200
        tickets = resp.json()["tickets"]
        assert set(tickets) == {"T-1", "T-2"}


class TestTicketEventsEndpoint:
    async def test_get_ticket_events(self, client, store):
        await store.insert(Event(
            event_type="stage_transition",
            message="NEW -> ANALYSIS",
            ticket_id="T-1",
        ))
        await store.insert(Event(
            event_type="agent_completed",
            message="BA done",
            ticket_id="T-1",
            agent_id="ba-agent",
        ))
        resp = client.get("/api/tickets/T-1/events")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 2


class TestDashboardPage:
    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestTitleBackfill:
    def _write_state(self, root: Path, **fields):
        root.mkdir(parents=True, exist_ok=True)
        defaults = {
            "ticket_id": "T-1",
            "company_id": "acme",
            "repo_id": "acme-app",
            "current_state": "ANALYSIS",
            "started_at": "2026-04-27T00:00:00+00:00",
            "last_updated_at": "2026-04-27T00:00:00+00:00",
        }
        defaults.update(fields)
        (root / "state.json").write_text(json.dumps(defaults), encoding="utf-8")

    def test_backfills_from_ticket_md(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-1"
        self._write_state(ws)
        meta = ws / "meta"
        meta.mkdir()
        (meta / "ticket.md").write_text(
            "# T-1: Login screen flickers on cold start\n\n## Description\n",
            encoding="utf-8",
        )

        results = _scan_all_workspaces(str(tmp_path))

        assert results[0]["title"] == "Login screen flickers on cold start"
        # Disk was updated
        on_disk = json.loads((ws / "state.json").read_text())
        assert on_disk["title"] == "Login screen flickers on cold start"

    def test_backfill_handles_missing_id_prefix(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-2"
        self._write_state(ws, ticket_id="T-2")
        meta = ws / "meta"
        meta.mkdir()
        (meta / "ticket.md").write_text("# Just a plain title\n", encoding="utf-8")

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == "Just a plain title"

    def test_backfill_setup_workspace(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "setup"
        self._write_state(ws, ticket_id="setup")
        # No meta/ticket.md

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == "Workspace setup"
        on_disk = json.loads((ws / "state.json").read_text())
        assert on_disk["title"] == "Workspace setup"

    def test_backfill_skipped_when_title_present(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-3"
        self._write_state(ws, ticket_id="T-3", title="Already set")
        meta = ws / "meta"
        meta.mkdir()
        (meta / "ticket.md").write_text("# T-3: Different title\n", encoding="utf-8")

        results = _scan_all_workspaces(str(tmp_path))
        assert results[0]["title"] == "Already set"

    def test_backfill_falls_back_to_ticket_id(self, tmp_path):
        ws = tmp_path / "acme" / "acme-app" / "tickets" / "T-4"
        self._write_state(ws, ticket_id="T-4")
        # No meta/ticket.md and not a setup dir

        results = _scan_all_workspaces(str(tmp_path))
        # API exposes ticket_id as a soft fallback
        assert results[0]["title"] == "T-4"
        # But state.json on disk is NOT polluted with ticket_id-as-title
        on_disk = json.loads((ws / "state.json").read_text())
        assert "title" not in on_disk or on_disk["title"] == ""


def test_static_files_are_revalidated(client):
    """A deploy changes the ES modules together: a browser must not link a cached old module to a new one (a
    missing export blanks the whole app until a hard reload), so every static file is revalidated."""
    r = client.get("/static/js/app.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_auto_refresh_label_names_its_interval(client):
    """The toolbar's checkbox names the period app.js refreshes at; the two drifted once (label 5 s, timer 30 s)."""
    label = re.search(r"Auto \((\d+)s\)", client.get("/").text)
    timer = re.search(r"function scheduleAutoRefresh\(\).*?\},\s*(\d+)\);", client.get("/static/js/app.js").text, re.S)
    assert label and timer
    assert int(label.group(1)) * 1000 == int(timer.group(1))


def test_scan_rows_carry_the_question_the_comments_and_the_escalation_message(tmp_path):
    ws_dir = tmp_path / "acme" / "acme-app" / "tickets" / "ACME-123"
    ws_dir.mkdir(parents=True)
    (ws_dir / "state.json").write_text(json.dumps({
        "ticket_id": "ACME-123", "company_id": "acme", "current_state": "PR_REVIEW",
        "human_input_question": "Which file?", "escalation_msg_id": 77,
        "pending_review_comments": [{"comment_id": "9", "author": "octo", "file": "a.py", "line": 3,
                                     "body": "b" * 400, "assessment": "valid", "decision": None,
                                     "msg_ids": [1, 2]}],
    }))
    row = _scan_all_workspaces(str(tmp_path))[0]
    assert row["human_input_question"] == "Which file?" and row["escalation_msg_id"] == 77
    assert row["pending_review_comments"] == [{
        "comment_id": "9", "author": "octo", "file": "a.py", "line": 3, "body": "b" * 300,
        "assessment": "valid", "decision": None, "hint_rounds": 0}]
