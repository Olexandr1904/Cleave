"""The Telegram language on the dashboard: the YAML edit, the Settings routes, and the wizard's choice."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from config.config_loader import load_config
from dashboard.project_create_payload import PayloadValidationError, redact_to_input_md, validate_payload
from dashboard.telegram_language import YamlEditError, build_language_routes, set_yaml_language

PROJECT = """\
# acme's project
project:
  id: "acme"
  name: "Acme"

telegram:
  # the operator chat
  bot_token: "${ACME_TELEGRAM_BOT_TOKEN}"
  default_chat_id: "123"

parallelism:
  max_concurrent_tickets: 3
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_inserts_into_the_telegram_block_keeping_everything_else(tmp_path):
    path = _write(tmp_path / "project.yaml", PROJECT)
    path.chmod(0o640)

    set_yaml_language(path, "uk")

    text = path.read_text(encoding="utf-8")
    assert text == PROJECT.replace('telegram:\n', 'telegram:\n  language: "uk"\n')
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert yaml.safe_load(text)["telegram"]["bot_token"] == "${ACME_TELEGRAM_BOT_TOKEN}"


def test_replaces_an_existing_language(tmp_path):
    path = _write(tmp_path / "project.yaml", PROJECT.replace('  default_chat_id: "123"\n',
                                                             '  default_chat_id: "123"\n  language: en\n'))
    set_yaml_language(path, "uk")
    assert path.read_text(encoding="utf-8") == PROJECT.replace('  default_chat_id: "123"\n',
                                                               '  default_chat_id: "123"\n  language: "uk"\n')
    set_yaml_language(path, "uk")  # the same again: unchanged
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["telegram"]["language"] == "uk"


def test_keeps_a_comment_on_the_language_line(tmp_path):
    path = _write(tmp_path / "project.yaml", 'telegram:\n  language: en   # the operator reads English\n')
    set_yaml_language(path, "uk")
    assert path.read_text(encoding="utf-8") == 'telegram:\n  language: "uk"   # the operator reads English\n'


def test_appends_a_block_when_there_is_none(tmp_path):
    path = _write(tmp_path / "global.yaml", "os: mac\nlogging:\n  level: INFO")
    set_yaml_language(path, "uk")
    assert path.read_text(encoding="utf-8") == 'os: mac\nlogging:\n  level: INFO\ntelegram:\n  language: "uk"\n'


def test_fills_an_empty_block(tmp_path):
    path = _write(tmp_path / "global.yaml", "telegram:\nlogging:\n  level: INFO\n")
    set_yaml_language(path, "en")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"telegram": {"language": "en"},
                                                                "logging": {"level": "INFO"}}


def test_refuses_a_flow_style_block_and_leaves_the_file(tmp_path):
    text = 'telegram: {default_chat_id: "1"}\n'
    path = _write(tmp_path / "global.yaml", text)
    with pytest.raises(YamlEditError):
        set_yaml_language(path, "uk")
    assert path.read_text(encoding="utf-8") == text


def test_refuses_a_language_cleave_does_not_write_in(tmp_path):
    path = _write(tmp_path / "project.yaml", PROJECT)
    with pytest.raises(ValueError):
        set_yaml_language(path, "de")
    assert path.read_text(encoding="utf-8") == PROJECT


# --- Routes ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_TELEGRAM_BOT_TOKEN", "1:not_real")
    (tmp_path / "global.yaml").write_text("logging:\n  level: INFO\n", encoding="utf-8")
    for name in ("acme", "beta"):
        d = tmp_path / "projects" / name
        d.mkdir(parents=True)
        (d / "project.yaml").write_text(PROJECT.replace("acme", name), encoding="utf-8")
    global_config, projects = load_config(str(tmp_path))
    return tmp_path, global_config, projects


def _client(config) -> TestClient:
    config_dir, global_config, projects = config
    return TestClient(Starlette(routes=build_language_routes(global_config, projects, str(config_dir))))


JSON = {"Content-Type": "application/json"}


def test_get_lists_the_global_and_each_project(config):
    config_dir, global_config, projects = config
    projects["beta"].config.telegram.language = "uk"
    got = _client(config).get("/api/settings/telegram-language").json()
    assert got == {"options": ["en", "uk"], "global": "en", "projects": [
        {"id": "acme", "name": "Acme", "language": "", "effective": "en"},
        {"id": "beta", "name": "Acme", "language": "uk", "effective": "uk"},
    ]}


def test_put_a_project_writes_its_yaml_and_applies_at_once(config):
    config_dir, global_config, projects = config
    r = _client(config).put("/api/settings/telegram-language", json={"project_id": "acme", "language": "uk"},
                            headers=JSON)
    assert r.status_code == 200 and r.json() == {"language": "uk", "effective": "uk"}
    assert projects["acme"].config.telegram.language == "uk"
    on_disk = yaml.safe_load((config_dir / "projects" / "acme" / "project.yaml").read_text(encoding="utf-8"))
    assert on_disk["telegram"]["language"] == "uk"
    assert load_config(str(config_dir))[1]["acme"].config.telegram.language == "uk"


def test_put_the_global_language(config):
    config_dir, global_config, projects = config
    r = _client(config).put("/api/settings/telegram-language", json={"project_id": None, "language": "uk"},
                            headers=JSON)
    assert r.status_code == 200 and global_config.telegram.language == "uk"
    assert yaml.safe_load((config_dir / "global.yaml").read_text(encoding="utf-8"))["telegram"] == {"language": "uk"}
    got = _client(config).get("/api/settings/telegram-language").json()
    assert got["global"] == "uk" and got["projects"][0]["effective"] == "uk"


@pytest.mark.parametrize("body, status", [
    ({"project_id": "acme", "language": "de"}, 400),
    ({"project_id": "acme"}, 400),
    ({"project_id": "gone", "language": "uk"}, 404),
])
def test_put_refuses(config, body, status):
    config_dir, global_config, projects = config
    before = (config_dir / "projects" / "acme" / "project.yaml").read_text(encoding="utf-8")
    r = _client(config).put("/api/settings/telegram-language", json=body, headers=JSON)
    assert r.status_code == status
    assert (config_dir / "projects" / "acme" / "project.yaml").read_text(encoding="utf-8") == before
    assert projects["acme"].config.telegram.language == ""


def test_put_refuses_a_cross_site_request(config):
    config_dir, global_config, projects = config
    client = _client(config)
    r = client.put("/api/settings/telegram-language", content='{"project_id": "acme", "language": "uk"}',
                   headers={"Content-Type": "text/plain"})
    assert r.status_code == 403
    r = client.put("/api/settings/telegram-language", json={"project_id": "acme", "language": "uk"},
                   headers={**JSON, "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert projects["acme"].config.telegram.language == ""


# --- The wizard -----------------------------------------------------------------------------------------------------

def _payload(language=None):
    extras = {"telegram_chat_id": "123", "protected_files": []}
    if language is not None:
        extras["telegram_language"] = language
    return {
        "identity": {"project_id": "acme", "display_name": "Acme", "repo_id": "app", "repo_display_name": "App"},
        "tracker": {"provider": "jira", "jira": {"url": "https://acme.example", "project_key": "AC",
                                                 "email": "bot@acme.example", "token": "t",
                                                 "trigger_labels": ["ai-pipeline"]}},
        "vcs": {"provider": "github", "github": {"owner": "acme", "repo": "app", "token": "t"}},
        "extras": extras,
    }


def test_create_writes_the_wizards_language_once_the_agent_wrote_the_project(tmp_path, monkeypatch):
    import copy
    import time

    from tests.integration.test_project_create_flow import VALID_PAYLOAD, _make_app

    monkeypatch.delenv("ACME_JIRA_TOKEN", raising=False)
    monkeypatch.delenv("ACME_GITHUB_TOKEN", raising=False)

    async def fake_atlas(workspace, config_dir):
        proj = config_dir / "projects" / workspace.project_id
        (proj / "repos").mkdir(parents=True)
        (proj / "project.yaml").write_text(PROJECT, encoding="utf-8")

    payload = copy.deepcopy(VALID_PAYLOAD)
    payload["extras"]["telegram_language"] = "uk"
    resp = TestClient(_make_app(tmp_path, fake_atlas)).post("/api/projects/create", json=payload)
    assert resp.status_code == 202
    project_yaml = tmp_path / "config-live" / "projects" / "acme" / "project.yaml"
    deadline = time.time() + 2
    while time.time() < deadline and 'language: "uk"' not in (
            project_yaml.read_text(encoding="utf-8") if project_yaml.exists() else ""):
        time.sleep(0.05)
    assert project_yaml.read_text(encoding="utf-8") == PROJECT.replace('telegram:\n', 'telegram:\n  language: "uk"\n')
    meta = tmp_path / "workspace" / "acme" / "acme-app" / "setup" / "meta" / "input.md"
    assert "- telegram_language: uk\n" in meta.read_text(encoding="utf-8")


def test_wizard_language_is_validated_and_listed():
    validate_payload(_payload("uk"))
    validate_payload(_payload())
    with pytest.raises(PayloadValidationError) as e:
        validate_payload(_payload("ua"))
    assert "extras.telegram_language" in e.value.field_errors
    assert "- telegram_language: uk\n" in redact_to_input_md(_payload("uk"))
    assert "telegram_language" not in redact_to_input_md(_payload())  # none chosen: the global one


def test_create_without_a_language_leaves_the_project_on_the_global_one(tmp_path, monkeypatch):
    import time

    from tests.integration.test_project_create_flow import VALID_PAYLOAD, _make_app

    monkeypatch.delenv("ACME_JIRA_TOKEN", raising=False)
    monkeypatch.delenv("ACME_GITHUB_TOKEN", raising=False)
    done = []

    async def fake_atlas(workspace, config_dir):
        proj = config_dir / "projects" / workspace.project_id
        (proj / "repos").mkdir(parents=True)
        (proj / "project.yaml").write_text(PROJECT, encoding="utf-8")
        done.append(True)

    from dashboard import project_create as pc

    resp = TestClient(_make_app(tmp_path, fake_atlas)).post("/api/projects/create", json=VALID_PAYLOAD)
    assert resp.status_code == 202
    deadline = time.time() + 2
    while time.time() < deadline and (not done or pc._busy):
        time.sleep(0.05)
    assert not pc._busy
    assert (tmp_path / "config-live" / "projects" / "acme" / "project.yaml").read_text(encoding="utf-8") == PROJECT
