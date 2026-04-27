"""Tests for main.py startup branching with zero projects."""

from __future__ import annotations

from unittest.mock import patch

import main


def test_main_runs_dashboard_with_zero_projects(tmp_path):
    """With zero projects, main() must NOT early-exit before the dashboard.

    Before the fix, main.py returned 0 immediately when projects was empty.
    After: asyncio.run(_run_all()) must be reached.
    """
    cfg = tmp_path / "config-live"
    (cfg / "projects").mkdir(parents=True)
    global_yaml = _MINIMAL_GLOBAL_YAML.format(
        ws=tmp_path / "ws",
        log=tmp_path / "log",
        db=tmp_path / "events.db",
    )
    (cfg / "global.yaml").write_text(global_yaml, encoding="utf-8")

    reached = {"run_all": False}

    def fake_asyncio_run(coro):
        reached["run_all"] = True
        coro.close()

    with patch("asyncio.run", side_effect=fake_asyncio_run):
        rc = main.main(["--config", str(cfg)])

    assert reached["run_all"], (
        "main() must reach asyncio.run(_run_all()) even with zero projects"
    )
    assert rc == 0


_MINIMAL_GLOBAL_YAML = """\
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
  max_iterations:
    scope_guard: 1
    fix: 1
    qa: 1
    dev: 1
  max_parallel_tickets: 1
  pr_comment_fetch_delay_minutes: 30
logging:
  level: WARNING
  dir: {log}
heartbeat:
  enabled: false
  interval_hours: 24
  send_at: '09:00'
operator:
  role: ''
  stack: []
  preferences:
    code_style: ''
    commit_format: ''
  rules: []
"""
