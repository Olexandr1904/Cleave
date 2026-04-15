import pytest

from config.schemas import JiraConfig


def test_jira_config_has_trigger_labels_list():
    cfg = JiraConfig()
    assert cfg.trigger_labels == ["ai-pipeline"]
    assert not hasattr(cfg, "trigger_label")


def test_jira_config_accepts_multiple_labels():
    cfg = JiraConfig(trigger_labels=["ai-pipeline", "acme-mobile-android"])
    assert cfg.trigger_labels == ["ai-pipeline", "acme-mobile-android"]
