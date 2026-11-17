from __future__ import annotations

import pytest
import yaml

from config.config_loader import ConfigError, load_config

REPO = {"vcs": {"provider": "github", "github": {"token": "${GITHUB_TOKEN}", "owner": "acme", "repo": "app"}}}

VALID = {"bug_skill": "fix-this", "task_skill": "implement-this", "notes_dir": "ai_instructions/{key}",
         "worktree_dir": ".claude/worktrees/{key}"}


@pytest.fixture(autouse=True)
def _github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


def load_repo(tmp_path, overrides, global_yaml="os: mac\n", project_yaml="project:\n  id: p1\n"):
    """The repo loaded through the whole hierarchy: a minimal repo YAML with `overrides` as its top-level keys."""
    (tmp_path / "global.yaml").write_text(global_yaml)
    pdir = tmp_path / "projects" / "p1"
    (pdir / "repos").mkdir(parents=True)
    (pdir / "project.yaml").write_text(project_yaml)
    (pdir / "repos" / "r1.yaml").write_text(yaml.safe_dump({**REPO, **overrides}))
    _, projects = load_config(str(tmp_path))
    return projects["p1"].repos["r1"]


def test_default_is_staged_and_unvalidated(tmp_path):
    repo = load_repo(tmp_path, {"skill_flow": {"notes_dir": "/abs"}})   # not validated: flow is staged
    assert repo.ticket_flow == "staged"

def test_both_parses_the_section(tmp_path):
    repo = load_repo(tmp_path, {"ticket_flow": "both", "skill_flow": {**VALID, "gates": {"test": "./gradlew test"}}})
    assert repo.ticket_flow == "both" and repo.skill_flow.gate_rounds == 2
    assert repo.skill_flow.timeout_seconds == 14400 and repo.skill_flow.bug_types == ["Bug"]

@pytest.mark.parametrize("change, field", [
    ({"ticket_flow": "skill"}, "ticket_flow"),
    ({"ticket_flow": "both", "skill_flow": None}, "skill_flow"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "bug_skill": ""}}, "skill_flow.bug_skill"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "task_skill": "a b"}}, "skill_flow.task_skill"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "notes_dir": "notes"}}, "skill_flow.notes_dir"),          # no {key}
    ({"ticket_flow": "both", "skill_flow": {**VALID, "notes_dir": "/abs/{key}"}}, "skill_flow.notes_dir"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "worktree_dir": "../wt/{key}"}}, "skill_flow.worktree_dir"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "worktree_dir": "{key}/{pr}"}}, "skill_flow.worktree_dir"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "required_files": ["../x"]}}, "skill_flow.required_files"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "gates": {"deploy": "x"}}}, "skill_flow.gates"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "gate_rounds": -1}}, "skill_flow.gate_rounds"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "timeout_seconds": 0}}, "skill_flow.timeout_seconds"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "build_env": ["GITHUB_TOKEN"]}}, "skill_flow.build_env"),  # the repo's token variable
    ({"ticket_flow": "both", "skill_flow": {**VALID, "build_env": ["ANTHROPIC_API_KEY"]}}, "skill_flow.build_env"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "unknown": 1}}, "skill_flow"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "host_notes": ["No emulator"]}}, "skill_flow.host_notes"),
    ({"ticket_flow": "both", "skill_flow": {**VALID, "effort": 5}}, "skill_flow.effort"),
])
def test_bad_config_names_the_field(tmp_path, change, field):
    with pytest.raises(ConfigError) as e:
        load_repo(tmp_path, change)
    assert e.value.field == field

def test_both_on_gitlab_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="only GitHub"):
        load_repo(tmp_path, {"ticket_flow": "both", "skill_flow": VALID, "vcs": {"provider": "gitlab"}})

def test_bug_types_as_a_bare_string_is_an_error(tmp_path):
    """A bare string would be matched character by character."""
    with pytest.raises(ConfigError) as e:
        load_repo(tmp_path, {"ticket_flow": "both", "skill_flow": {**VALID, "bug_types": "Bug"}})
    assert e.value.field == "skill_flow.bug_types"

def test_build_env_refuses_a_variable_the_project_config_reads(tmp_path, monkeypatch):
    """The tracker token is read in project.yaml: the session holds no tracker variable."""
    monkeypatch.setenv("JIRA_TOKEN", "test-jira-token")
    project_yaml = ('project:\n  id: p1\ntracker:\n  provider: jira\n  jira:\n    url: https://acme.example\n'
                    '    token: "${JIRA_TOKEN}"\n')
    with pytest.raises(ConfigError) as e:
        load_repo(tmp_path, {"ticket_flow": "both", "skill_flow": {**VALID, "build_env": ["JIRA_TOKEN"]}},
                  project_yaml=project_yaml)
    assert e.value.field == "skill_flow.build_env"

def test_build_env_refuses_every_variable_a_config_string_reads(tmp_path, monkeypatch):
    """Not only the first ${VAR} of a string: the token here is its second."""
    monkeypatch.setenv("GH_USER", "octo"); monkeypatch.setenv("GH_TOKEN", "test-gh-token")
    git = {"clone_url": "https://${GH_USER}:${GH_TOKEN}@github.com/acme/app.git"}
    with pytest.raises(ConfigError, match="must not pass GH_TOKEN") as e:
        load_repo(tmp_path, {"git": git, "ticket_flow": "both", "skill_flow": {**VALID, "build_env": ["GH_TOKEN"]}})
    assert e.value.field == "skill_flow.build_env"

@pytest.mark.parametrize("level", ["global", "project"])
def test_build_env_refuses_a_later_variable_of_the_global_or_project_config(tmp_path, monkeypatch, level):
    monkeypatch.setenv("FIRST", "a"); monkeypatch.setenv("SECOND", "b")
    yaml_text = 'telegram:\n  bot_token: "${FIRST}:${SECOND}"\n'
    kwargs = ({"global_yaml": "os: mac\n" + yaml_text} if level == "global"
              else {"project_yaml": "project:\n  id: p1\n" + yaml_text})
    with pytest.raises(ConfigError, match="must not pass SECOND"):
        load_repo(tmp_path, {"ticket_flow": "both", "skill_flow": {**VALID, "build_env": ["SECOND"]}}, **kwargs)

@pytest.mark.parametrize("notes_dir, worktree_dir", [
    ("work/{key}", "work/{key}"),                                   # one directory
    ("work/{key}", "work/{key}/tree"),                              # the worktree inside the notes
    (".claude/worktrees/{key}/notes", ".claude/worktrees/{key}"),   # the notes inside the worktree
    ("Work/{key}", "work/{key}/"),                                  # the same on the host's APFS
])
def test_notes_and_worktree_directories_that_overlap_are_an_error(tmp_path, notes_dir, worktree_dir):
    with pytest.raises(ConfigError) as e:
        load_repo(tmp_path, {"ticket_flow": "both",
                             "skill_flow": {**VALID, "notes_dir": notes_dir, "worktree_dir": worktree_dir}})
    assert "skill_flow.notes_dir" in str(e.value) and "skill_flow.worktree_dir" in str(e.value)

def test_notes_and_worktree_directories_side_by_side_are_fine(tmp_path):
    repo = load_repo(tmp_path, {"ticket_flow": "both", "skill_flow": {
        **VALID, "notes_dir": "work/{key}-notes", "worktree_dir": "work/{key}"}})
    assert repo.skill_flow.worktree_dir == "work/{key}"

def test_build_env_refuses_a_variable_the_global_config_reads(tmp_path, monkeypatch):
    """The Telegram bot token is read in global.yaml: the session holds no Telegram variable."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    global_yaml = 'os: mac\ntelegram:\n  bot_token: "${TELEGRAM_BOT_TOKEN}"\n'
    with pytest.raises(ConfigError) as e:
        load_repo(tmp_path, {"ticket_flow": "both", "skill_flow": {**VALID, "build_env": ["TELEGRAM_BOT_TOKEN"]}},
                  global_yaml=global_yaml)
    assert e.value.field == "skill_flow.build_env"
