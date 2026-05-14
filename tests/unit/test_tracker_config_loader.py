"""Tests for tracker config loading: new shape, legacy shape, and back-compat shim."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.config_loader import load_config
from config.schemas import TrackerConfig


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def make_project(tmp_path: Path):
    def _make(project_id: str, project_yaml: str, repo_yaml: str = "") -> Path:
        config_dir = tmp_path / "config-live"
        proj_dir = config_dir / "projects" / project_id
        _write_yaml(proj_dir / "project.yaml", project_yaml)
        if repo_yaml:
            _write_yaml(proj_dir / "repos" / "main.yaml", repo_yaml)
        # global.yaml is required by load_config
        _write_yaml(config_dir / "global.yaml", "telegram:\n  bot_token: ''\n")
        return config_dir
    return _make


def test_legacy_jira_block_lifted_to_tracker(make_project, caplog):
    """A project.yaml with only a top-level `jira:` block loads as tracker.jira with provider=jira."""
    config_dir = make_project("acme", """
project:
  id: acme
  name: Acme
jira:
  url: https://acme.atlassian.net
  email: bot@acme.com
  token: secret
  project_key: ACME
  trigger_labels: [ai-pipeline]
""")
    with caplog.at_level("INFO"):
        _, projects = load_config(str(config_dir))
    cfg = projects["acme"].config.tracker
    assert cfg.provider == "jira"
    assert cfg.jira.url == "https://acme.atlassian.net"
    assert cfg.jira.project_key == "ACME"
    assert cfg.trello.api_key == ""   # default
    assert any("migrated legacy" in rec.message for rec in caplog.records)


def test_new_tracker_block_jira(make_project):
    config_dir = make_project("acme", """
project:
  id: acme
  name: Acme
tracker:
  provider: jira
  jira:
    url: https://acme.atlassian.net
    email: bot@acme.com
    token: secret
    project_key: ACME
    trigger_labels: [ai-pipeline]
""")
    _, projects = load_config(str(config_dir))
    cfg = projects["acme"].config.tracker
    assert cfg.provider == "jira"
    assert cfg.jira.project_key == "ACME"


def test_new_tracker_block_trello(make_project):
    config_dir = make_project("marketing", """
project:
  id: marketing
  name: Marketing
tracker:
  provider: trello
  trello:
    api_key: kkk
    token: ttt
    board_id: abc123
    trigger_labels: [ai-pipeline]
    lists:
      todo: list-id-1
      in_progress: list-id-2
      in_review: list-id-3
      done: list-id-4
""")
    _, projects = load_config(str(config_dir))
    cfg = projects["marketing"].config.tracker
    assert cfg.provider == "trello"
    assert cfg.trello.api_key == "kkk"
    assert cfg.trello.board_id == "abc123"
    assert cfg.trello.lists.todo == "list-id-1"
    assert cfg.trello.lists.done == "list-id-4"


def test_both_blocks_present_tracker_wins(make_project, caplog):
    """When both `tracker:` and legacy `jira:` are present, `tracker:` wins; WARN logged."""
    config_dir = make_project("acme", """
project:
  id: acme
  name: Acme
tracker:
  provider: jira
  jira:
    url: https://new.atlassian.net
    email: bot@new.com
    token: secret
    project_key: NEW
jira:
  url: https://old.atlassian.net
  email: ignored@old.com
  token: ignored
  project_key: OLD
""")
    with caplog.at_level("WARNING"):
        _, projects = load_config(str(config_dir))
    cfg = projects["acme"].config.tracker
    assert cfg.jira.project_key == "NEW"
    assert any("legacy 'jira:' blocks" in rec.message or "legacy 'jira:' block" in rec.message for rec in caplog.records)


def test_repo_inherits_tracker(make_project):
    config_dir = make_project(
        "acme",
        project_yaml="""
project: {id: acme, name: Acme}
tracker:
  provider: jira
  jira:
    url: https://acme.atlassian.net
    email: bot@acme.com
    token: secret
    project_key: ACME
""",
        repo_yaml="""
repo: {id: main, name: main-repo}
vcs:
  provider: github
  github:
    token: gh-token
    owner: acme
    repo: app
tracker_label: ai-pipeline
""",
    )
    _, projects = load_config(str(config_dir))
    repo = projects["acme"].repos["main"]
    assert repo.tracker.provider == "jira"
    assert repo.tracker.jira.project_key == "ACME"


def test_unknown_provider_raises(make_project):
    config_dir = make_project("acme", """
project: {id: acme, name: Acme}
tracker:
  provider: unknown-thing
""")
    from config.config_loader import ConfigError
    with pytest.raises(ConfigError):
        load_config(str(config_dir))


def test_invalid_trello_lists_key_raises_clean_error(make_project):
    """An unknown key under tracker.trello.lists surfaces as ConfigError, not bare TypeError."""
    from config.config_loader import ConfigError
    config_dir = make_project("marketing", """
project: {id: marketing, name: Marketing}
tracker:
  provider: trello
  trello:
    api_key: k
    token: t
    board_id: b
    lists:
      todo: L1
      bogus_status: L9
""")
    with pytest.raises(ConfigError) as exc:
        load_config(str(config_dir))
    assert "tracker.trello.lists" in str(exc.value) or "lists" in str(exc.value)


def test_unknown_tracker_top_level_key_raises(make_project):
    """A misspelled top-level key under `tracker:` raises ConfigError instead of loading empty."""
    from config.config_loader import ConfigError
    config_dir = make_project("acme", """
project: {id: acme, name: Acme}
tracker:
  provider: jira
  jora:
    url: https://acme.atlassian.net
""")
    with pytest.raises(ConfigError) as exc:
        load_config(str(config_dir))
    assert "tracker" in str(exc.value)
