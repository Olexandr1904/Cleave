from __future__ import annotations
import pytest
from config.config_loader import ConfigError, load_config, load_global_config

def _global(tmp_path, text):
    (tmp_path / "global.yaml").write_text(text)
    return load_global_config(str(tmp_path))

def test_global_sections_pipeline_intent_dashboard_parsed(tmp_path):
    cfg = _global(tmp_path, "pipeline:\n  mode: auto\nintent_parser:\n  timeout_seconds: 7\n"
                            "dashboard:\n  host: 127.0.0.1\n  port: 9191\n")
    assert cfg.pipeline.mode == "auto"
    assert cfg.intent_parser.timeout_seconds == 7
    assert (cfg.dashboard.host, cfg.dashboard.port) == ("127.0.0.1", 9191)

def test_unknown_dashboard_field_names_the_section(tmp_path):
    with pytest.raises(ConfigError, match="dashboard"):
        _global(tmp_path, "dashboard:\n  colour: red\n")

def test_reviewer_global_defaults(tmp_path):
    r = _global(tmp_path, "os: mac\n").reviewer
    assert (r.db_path, r.telegram_chat_id) == ("", "")
    assert (r.jobs["review"].model, r.jobs["review"].effort, r.jobs["review"].timeout_seconds) == ("opus", "xhigh", 3600)
    assert (r.jobs["recheck"].model, r.jobs["recheck"].effort) == ("opus", "xhigh")
    assert (r.jobs["edit"].model, r.jobs["edit"].timeout_seconds) == ("sonnet", 300)
    assert (r.jobs["command"].model, r.jobs["command"].timeout_seconds) == ("sonnet", 60)

def test_reviewer_global_job_override_merges(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/h")
    r = _global(tmp_path, 'reviewer:\n  db_path: "${HOME}/x/reviewer.db"\n'
                          "  jobs:\n    review:\n      timeout_seconds: 5400\n").reviewer
    assert r.db_path == "/h/x/reviewer.db"
    assert (r.jobs["review"].model, r.jobs["review"].effort, r.jobs["review"].timeout_seconds) == ("opus", "xhigh", 5400)

def test_reviewer_global_unknown_job_kind_raises(tmp_path):
    with pytest.raises(ConfigError, match="reviewer.jobs"):
        _global(tmp_path, "reviewer:\n  jobs:\n    deploy:\n      model: opus\n")

def test_env_refs_record_variable_names(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_T", "secret-1")
    monkeypatch.setenv("GH_T", "secret-2")
    monkeypatch.setenv("JI_T", "secret-3")
    (tmp_path / "global.yaml").write_text('telegram:\n  bot_token: "${BOT_T}"\n')
    pdir = tmp_path / "projects" / "p1"
    (pdir / "repos").mkdir(parents=True)
    (pdir / "project.yaml").write_text(
        'project:\n  id: p1\ntracker:\n  provider: jira\n  jira:\n    url: https://x\n    token: "${JI_T}"\n')
    (pdir / "repos" / "r1.yaml").write_text(
        'vcs:\n  provider: github\n  github:\n    token: "${GH_T}"\n    owner: o\n    repo: r\n')
    g, projects = load_config(str(tmp_path))
    assert g.env_refs["telegram.bot_token"] == "BOT_T"
    assert projects["p1"].config.env_refs["tracker.jira.token"] == "JI_T"
    assert projects["p1"].repos["r1"].env_refs["vcs.github.token"] == "GH_T"
    everything = repr(g.env_refs) + repr(projects["p1"].config.env_refs) + repr(projects["p1"].repos["r1"].env_refs)
    assert "secret" not in everything


from pathlib import Path
import yaml
from config.config_loader import _parse_reviewer_section
from config.schemas import VCSConfig

FULL = """
reviewer:
  enabled: true
  base_branch: develop
  readiness:
    kind: command
    status_command: ["python3", "tools/threads/threads.py", "--repo", "{repo}", "status"]
    threads_command: ["python3", "tools/threads/threads.py", "--repo", "{repo}", "threads", "{pr}"]
  process:
    doc: docs/review/code-review-process.md
    run_dir: "tools/review/runs/PR_{pr}"
    replies_plan: "tools/threads/runs/PR_{pr}/replies_{head9}.json"
    worktree_dir: "../review-wt/PR_{pr}"
    prepare_steps:
      - ["python3", "tools/review/anchors.py", "{pr}", "--worktree", "{worktree}", "--base", "{merge_base}"]
    helper_scripts: ["tools/review/*.py"]
  poster:
    kind: command
    review_command: ["python3", "tools/review/post.py", "{pr}", "--repo", "{repo}", "--report", "{report}",
                     "--anchors", "{job_dir}/anchors.json", "--ledger", "{ledger}", "--event", "{event}"]
    replies_command: ["python3", "tools/threads/threads.py", "--repo", "{repo}", "post", "{pr}", "--plan", "{plan}"]
"""

def _section(text):
    return yaml.safe_load(text)

def _vcs(provider="github"):
    return VCSConfig(provider=provider, default_branch="main")

def _with(path_and_value):
    data = _section(FULL)
    node = data["reviewer"]
    *parents, leaf = path_and_value[0].split(".")
    for p in parents:
        node = node[p]
    node[leaf] = path_and_value[1]
    return data

def test_reviewer_section_parsed():
    r = _parse_reviewer_section(_section(FULL), _vcs(), "r.yaml")
    assert r.enabled and r.base_branch == "develop"
    assert (r.poll_interval_seconds, r.quiet_minutes, r.max_files_auto) == (300, 30, 40)
    assert r.readiness.status_command[-1] == "status"
    assert r.process.prepare_section == "Prepare" and r.process.recheck_section == "Re-check"
    assert r.process.prepare_steps[0][1] == "tools/review/anchors.py"
    assert (r.poster.dry_run_flag, r.poster.confirm_flag) == ("--dry-run", "--yes")

def test_reviewer_absent_defaults_disabled_with_vcs_branch():
    r = _parse_reviewer_section({}, _vcs(), "r.yaml")
    assert not r.enabled and r.base_branch == "main"

def test_reviewer_disabled_partial_section_loads():
    r = _parse_reviewer_section({"reviewer": {"enabled": False, "readiness": {"kind": "rule"}}}, _vcs(), "r.yaml")
    assert not r.enabled

def test_reviewer_unknown_field_raises_even_when_disabled():
    with pytest.raises(ConfigError, match="reviewer"):
        _parse_reviewer_section({"reviewer": {"enabled": False, "quiet_minuts": 5}}, _vcs(), "r.yaml")

def test_reviewer_on_gitlab_repo_raises():
    with pytest.raises(ConfigError, match="GitHub"):
        _parse_reviewer_section(_section(FULL), _vcs("gitlab"), "r.yaml")

def test_reviewer_rule_kind_raises():
    with pytest.raises(ConfigError, match="not built"):
        _parse_reviewer_section(_with(("readiness.kind", "rule")), _vcs(), "r.yaml")

def test_reviewer_missing_doc_raises():
    with pytest.raises(ConfigError, match="reviewer.process.doc"):
        _parse_reviewer_section(_with(("process.doc", "")), _vcs(), "r.yaml")

def test_reviewer_doc_outside_repo_raises():
    with pytest.raises(ConfigError, match="repo-relative"):
        _parse_reviewer_section(_with(("process.doc", "../x.md")), _vcs(), "r.yaml")

def test_reviewer_unknown_placeholder_raises():
    with pytest.raises(ConfigError, match="prr"):
        _parse_reviewer_section(_with(("readiness.status_command", ["python3", "t.py", "{prr}"])), _vcs(), "r.yaml")

def test_reviewer_command_must_be_list():
    with pytest.raises(ConfigError, match="list"):
        _parse_reviewer_section(_with(("readiness.status_command", "python3 t.py status")), _vcs(), "r.yaml")

def test_example_repo_reviewer_block_validates_when_enabled():
    path = Path("config-live.example/projects/example-project/repos/example-repo.yaml")
    data = yaml.safe_load(path.read_text())
    assert data["reviewer"]["enabled"] is False
    data["reviewer"]["enabled"] = True
    assert _parse_reviewer_section(data, _vcs(), str(path)).enabled


# --- Code-review fix round, batch B ---

def test_disabled_repo_skips_reviewer_validation(tmp_path):
    (tmp_path / "global.yaml").write_text("os: mac\n")
    pdir = tmp_path / "projects" / "p1"
    (pdir / "repos").mkdir(parents=True)
    (pdir / "project.yaml").write_text("project:\n  id: p1\n")
    (pdir / "repos" / "r1.yaml").write_text(
        "repo:\n  enabled: false\nvcs:\n  provider: gitlab\n"
        "reviewer:\n  enabled: true\n  readiness:\n    kind: rule\n")
    _, projects = load_config(str(tmp_path))
    assert projects["p1"].repos == {}

@pytest.mark.parametrize("data,field", [
    ({"reviewer": "yes"}, "reviewer"),
    ({"reviewer": ["enabled"]}, "reviewer"),
    ({"reviewer": {"enabled": True, "readiness": "command"}}, "reviewer.readiness"),
    ({"reviewer": {"enabled": True, "process": ["doc.md"]}}, "reviewer.process"),
    ({"reviewer": {"enabled": True, "poster": 5}}, "reviewer.poster"),
    (_with(("process.worktree_dir", 5)), "reviewer.process.worktree_dir"),
    (_with(("process.worktree_dir", ["../wt"])), "reviewer.process.worktree_dir"),
    (_with(("process.doc", 5)), "reviewer.process.doc"),
    (_with(("process.run_dir", ["runs"])), "reviewer.process.run_dir"),
    (_with(("process.replies_plan", {"a": 1})), "reviewer.process.replies_plan"),
    (_with(("process.helper_scripts", "tools/review/*.py")), "reviewer.process.helper_scripts"),
    (_with(("process.helper_scripts", ["tools/review/*.py", 5])), "reviewer.process.helper_scripts"),
])
def test_malformed_repo_reviewer_section_raises_config_error(data, field):
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(data, _vcs(), "r.yaml")
    assert (e.value.file_path, e.value.field) == ("r.yaml", field)

@pytest.mark.parametrize("text,field", [
    ("reviewer: yes-please\n", "reviewer"),
    ("reviewer:\n  - db_path\n", "reviewer"),
    ("reviewer:\n  jobs:\n    - review\n", "reviewer.jobs"),
    ("reviewer:\n  jobs: opus\n", "reviewer.jobs"),
    ("reviewer:\n  jobs:\n    review: opus\n", "reviewer.jobs.review"),
    ("reviewer:\n  jobs:\n    review:\n      - opus\n", "reviewer.jobs.review"),
])
def test_malformed_global_reviewer_section_raises_config_error(tmp_path, text, field):
    with pytest.raises(ConfigError) as e:
        _global(tmp_path, text)
    assert (e.value.file_path, e.value.field) == (str(tmp_path / "global.yaml"), field)
