"""telegram.language: the language Cleave writes in Telegram, per project and globally."""
from __future__ import annotations

import pytest

from config.config_loader import ConfigError, load_config
from orchestrator.tg_i18n import chat_language, project_language


def _write(tmp_path, global_yaml: str, projects: dict[str, str]) -> str:
    (tmp_path / "global.yaml").write_text(global_yaml)
    for name, text in projects.items():
        d = tmp_path / "projects" / name
        d.mkdir(parents=True)
        (d / "project.yaml").write_text(text)
    return str(tmp_path)


def test_project_language_loads(tmp_path):
    config_dir = _write(tmp_path, "logging:\n  level: INFO\n",
                        {"acme": "project:\n  id: acme\ntelegram:\n  language: uk\n"})
    global_config, projects = load_config(config_dir)
    assert global_config.telegram.language == ""
    assert projects["acme"].config.telegram.language == "uk"


def test_global_language_loads(tmp_path):
    global_config, _ = load_config(_write(tmp_path, "telegram:\n  language: uk\n", {}))
    assert global_config.telegram.language == "uk"


@pytest.mark.parametrize("where", ["global", "project"])
def test_unknown_language_is_a_config_error(tmp_path, where):
    bad = "telegram:\n  language: de\n"
    config_dir = _write(tmp_path, bad if where == "global" else "logging:\n  level: INFO\n",
                        {"acme": "project:\n  id: acme\n" + (bad if where == "project" else "")})
    with pytest.raises(ConfigError, match="telegram.language"):
        load_config(config_dir)


def test_project_language_falls_back_to_global_then_english(tmp_path):
    config_dir = _write(tmp_path, "telegram:\n  language: uk\n", {
        "acme": "project:\n  id: acme\ntelegram:\n  language: en\n",
        "beta": "project:\n  id: beta\n",
    })
    global_config, projects = load_config(config_dir)
    assert project_language(global_config, projects, "acme") == "en"
    assert project_language(global_config, projects, "beta") == "uk"
    assert project_language(global_config, projects, "") == "uk"
    assert project_language(global_config, projects, "gone") == "uk"
    global_config.telegram.language = ""
    assert project_language(global_config, projects, "beta") == "en"


def test_chat_language(tmp_path):
    config_dir = _write(tmp_path, "telegram:\n  default_chat_id: '1'\n", {
        "acme": "project:\n  id: acme\ntelegram:\n  default_chat_id: '2'\n  language: uk\n",
        "beta": "project:\n  id: beta\ntelegram:\n  default_chat_id: '2'\n  language: uk\n",
        "gamma": "project:\n  id: gamma\ntelegram:\n  default_chat_id: '3'\n  language: uk\n",
        "delta": "project:\n  id: delta\ntelegram:\n  default_chat_id: '3'\n",
    })
    global_config, projects = load_config(config_dir)
    assert chat_language(global_config, projects, "2") == "uk"  # its projects agree
    assert chat_language(global_config, projects, "3") == "en"  # they disagree: the global one
    assert chat_language(global_config, projects, "1") == "en"  # no project's chat
    global_config.telegram.language = "uk"
    assert chat_language(global_config, projects, "3") == "uk"
