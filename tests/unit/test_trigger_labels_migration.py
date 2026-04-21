import pytest

from config.schemas import JiraConfig


def test_jira_config_has_trigger_labels_list():
    cfg = JiraConfig()
    assert cfg.trigger_labels == ["ai-pipeline"]
    assert not hasattr(cfg, "trigger_label")


def test_jira_config_accepts_multiple_labels():
    cfg = JiraConfig(trigger_labels=["ai-pipeline", "acme-mobile"])
    assert cfg.trigger_labels == ["ai-pipeline", "acme-mobile"]


from pathlib import Path

from config.config_loader import ConfigError, _load_project_config


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
  token: "dummy-token"
  email: "x@example.com"
  project_key: "LEG"
  trigger_label: "ai-pipeline"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="trigger_label.*trigger_labels"):
        _load_project_config(project_dir, {})
