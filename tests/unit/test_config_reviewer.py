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
    assert (r.jobs["review"].model, r.jobs["review"].effort, r.jobs["review"].timeout_seconds) == ("opus", "xhigh", 5400)
    assert (r.jobs["recheck"].model, r.jobs["recheck"].effort) == ("opus", "xhigh")
    assert (r.jobs["edit"].model, r.jobs["edit"].timeout_seconds) == ("sonnet", 300)
    assert (r.jobs["command"].model, r.jobs["command"].timeout_seconds) == ("sonnet", 60)

def test_reviewer_global_job_override_merges(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/h")
    r = _global(tmp_path, 'reviewer:\n  db_path: "${HOME}/x/reviewer.db"\n'
                          "  jobs:\n    review:\n      timeout_seconds: 7200\n").reviewer
    assert r.db_path == "/h/x/reviewer.db"
    assert (r.jobs["review"].model, r.jobs["review"].effort, r.jobs["review"].timeout_seconds) == ("opus", "xhigh", 7200)

def test_max_parallel_jobs_defaults_to_6(tmp_path):
    assert _global(tmp_path, "os: mac\n").reviewer.max_parallel_jobs == 6

def test_max_parallel_jobs_read(tmp_path):
    assert _global(tmp_path, "reviewer:\n  max_parallel_jobs: 2\n").reviewer.max_parallel_jobs == 2

@pytest.mark.parametrize("value", ["0", "-1", "true", "two"])
def test_max_parallel_jobs_below_one_refused(tmp_path, value):
    with pytest.raises(ConfigError, match="reviewer.max_parallel_jobs"):
        _global(tmp_path, f"reviewer:\n  max_parallel_jobs: {value}\n")

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
from config.schemas import ReviewerGlobalConfig, VCSConfig

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

@pytest.mark.parametrize("field, command", [
    ("readiness.status_command", ["python3", "tools/review/runs/PR_7/evil.py", "--repo", "{repo}", "status"]),
    ("readiness.threads_command", ["sh", "-c", "./tools/review/runs/PR_{pr}/../PR_{pr}/threads.sh"]),
    ("poster.review_command", ["tools/threads/runs/PR_{pr}/post.sh", "{pr}"]),
    ("poster.replies_command", ["python3", "-u", "Tools/Threads/Runs/PR_7/post.py"]),   # APFS ignores case
    ("process.prepare_steps", [["python3", "tools/review/runs/PR_{pr}/anchors.py", "{pr}"]]),
])
def test_reviewer_command_running_a_script_the_session_may_write_raises(field, command):
    """The review session may write the run directory and the replies plan's: a command that runs a script
    from there would run the session's code, with the token for the readiness and poster commands."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((field, command)), _vcs(), "r.yaml")
    where = f"reviewer.{field}" + (".0" if field == "process.prepare_steps" else "")
    assert e.value.field == where and str(e.value).startswith(f"{where} runs ")

def test_reviewer_command_may_name_the_run_directory_as_data():
    data = _with(("poster.replies_command", ["python3", "tools/threads/threads.py", "post", "{pr}", "--plan", "{plan}",
                                             "--log", "tools/threads/runs/PR_{pr}/replies.json"]))
    data["reviewer"]["process"]["prepare_steps"] = [["python3", "tools/review/anchors.py",
                                                     "--out", "tools/review/runs/PR_{pr}/anchors.json"]]
    assert _parse_reviewer_section(data, _vcs(), "r.yaml").enabled

def test_review_job_default_timeout():
    jobs = ReviewerGlobalConfig().jobs
    assert (jobs["review"].timeout_seconds, jobs["recheck"].timeout_seconds) == (5400, 5400)

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

@pytest.mark.parametrize("name,value", [
    ("poll_interval_seconds", 59), ("poll_interval_seconds", 0), ("poll_interval_seconds", -300),
    ("poll_interval_seconds", "300"), ("poll_interval_seconds", 300.0),
    ("poll_interval_seconds", True),
    ("quiet_minutes", -1), ("quiet_minutes", "30"), ("quiet_minutes", False),
    ("max_files_auto", 0), ("max_files_auto", 40.5), ("max_files_auto", True),
])
def test_reviewer_numbers_out_of_range_raise(name, value):
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((name, value)), _vcs(), "r.yaml")
    assert e.value.field == f"reviewer.{name}" and f"reviewer.{name}" in str(e.value)

def test_reviewer_numbers_at_their_bounds_load():
    data = _with(("poll_interval_seconds", 60))
    data["reviewer"].update(quiet_minutes=0, max_files_auto=1)
    r = _parse_reviewer_section(data, _vcs(), "r.yaml")
    assert (r.poll_interval_seconds, r.quiet_minutes, r.max_files_auto) == (60, 0, 1)

def test_reviewer_numbers_unchecked_when_disabled():
    data = {"reviewer": {"enabled": False, "poll_interval_seconds": 0, "max_files_auto": 0}}
    assert _parse_reviewer_section(data, _vcs(), "r.yaml").poll_interval_seconds == 0

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


# --- Code-review fix wave 1 ---

@pytest.mark.parametrize("name, value", [
    ("run_dir", "."), ("run_dir", "./"),
    ("replies_plan", "replies_{pr}.json"), ("replies_plan", "./replies_{pr}.json"),
])
def test_reviewer_session_writable_path_at_the_repo_root_raises(name, value):
    """At the repo root the session's Edit rule would cover the whole clone, and the guard would look for
    an empty directory."""
    field = f"reviewer.process.{name}"
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((f"process.{name}", value)), _vcs(), "r.yaml")
    assert e.value.field == field and str(e.value).startswith(f"{field} must ") and "root" in str(e.value)

def test_reviewer_replies_plan_in_a_subdirectory_loads():
    data = _with(("process.replies_plan", "tools/review/replies_{pr}.json"))
    # A re-check session may write tools/review/ then: FULL's scripts there move out of it.
    data["reviewer"]["process"]["prepare_steps"] = [["python3", "tools/anchors.py", "{pr}"]]
    data["reviewer"]["poster"]["review_command"] = ["python3", "tools/post.py", "{pr}", "--report", "{report}",
                                                    "--ledger", "{ledger}", "--event", "{event}"]
    assert _parse_reviewer_section(data, _vcs(), "r.yaml").process.replies_plan == "tools/review/replies_{pr}.json"

@pytest.mark.parametrize("field, command", [
    ("process.prepare_steps", [["python3", "{worktree}/tools/gen.py"]]),
    ("readiness.threads_command", ["../review-wt/PR_{pr}/x.sh"]),
    ("poster.review_command", ["../review-wt/PR_{pr}/x.sh"]),
])
def test_reviewer_command_running_the_prs_own_code_raises(field, command):
    """Prepare steps run on the host with HOME, the readiness and poster commands with the forge token: a
    script in the worktree is the PR author's code."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((field, command)), _vcs(), "r.yaml")
    where = f"reviewer.{field}" + (".0" if field == "process.prepare_steps" else "")
    assert e.value.field == where and str(e.value).startswith(f"{where} runs ")
    assert "reviewer.process.worktree_dir" in str(e.value)

def test_reviewer_command_may_pass_the_worktree_to_a_host_program():
    step = ["detekt", "--input", "{worktree}"]
    assert _parse_reviewer_section(_with(("process.prepare_steps", [step])), _vcs(), "r.yaml").process.prepare_steps == [step]

def test_reviewer_worktree_dir_at_the_repo_root_raises():
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with(("process.worktree_dir", "./")), _vcs(), "r.yaml")
    assert e.value.field == "reviewer.process.worktree_dir" and "root" in str(e.value)

@pytest.mark.parametrize("field, command", [
    ("readiness.threads_command", ["env", "python3", "tools/review/runs/PR_{pr}/x.py"]),
    ("readiness.threads_command", ["python3", "-W", "ignore", "tools/review/runs/PR_{pr}/x.py"]),
    ("readiness.threads_command", ["/usr/bin/env", "python3", "tools/review/runs/PR_{pr}/x.py"]),
    ("poster.review_command", ["uv", "run", "tools/review/runs/PR_{pr}/x.py"]),
    ("poster.review_command", ["npx", "./tools/review/runs/PR_{pr}/cli.js"]),
    ("poster.review_command", ["python3", "tools/review/runs/PR_{pr}"]),   # runs its __main__.py
    ("process.prepare_steps", [["python3", "{worktree}"]]),
    ("process.prepare_steps", [["uv", "run", "--directory", "{worktree}", "pytest"]]),
    ("process.prepare_steps", [["sh", "-c", "cd tools && python3 {worktree}"]]),
    ("process.prepare_steps", [["timeout", "-s", "KILL", "30", "python3", "{worktree}"]]),   # a wrapper is transparent
])
def test_reviewer_command_running_untrusted_code_through_a_launcher_raises(field, command):
    """Any argument may be what a launcher runs, not only the first one that is no option."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((field, command)), _vcs(), "r.yaml")
    where = f"reviewer.{field}" + (".0" if field == "process.prepare_steps" else "")
    assert e.value.field == where and str(e.value).startswith(f"{where} runs ")

@pytest.mark.parametrize("field, command", [
    ("poster.review_command", ["python3", "tools/post.py", "{pr}", "--report", "{report}", "--ledger", "{ledger}",
                               "--event", "{event}", "--anchors", "tools/review/runs/PR_{pr}/anchors.json"]),
    ("readiness.threads_command", ["sh", "-c", "./tools/threads/run.sh {pr}"]),
    ("process.prepare_steps", [["python3", "-W", "ignore", "tools/review/anchors.py", "--worktree", "{worktree}"]]),
    ("process.prepare_steps", [["sh", "-c", "detekt --input {worktree} > tools/review/runs/PR_{pr}/detekt.txt"]]),
    ("process.prepare_steps", [["sh", "-c", "OUT=tools/review/runs/PR_{pr}/a.json python3 tools/review/anchors.py"]]),
    ("poster.review_command", ["timeout", "30", "python3", "tools/post.py", "{report}",
                               "--ledger", "{ledger}", "--event", "{event}"]),
])
def test_reviewer_command_reading_untrusted_data_loads(field, command):
    """A data file from an untrusted directory, or the worktree given to a script the base branch holds."""
    assert _parse_reviewer_section(_with((field, command)), _vcs(), "r.yaml").enabled

@pytest.mark.parametrize("field, value, placeholder", [
    ("process.worktree_dir", "../wt/{repo}/PR_{pr}", "repo"),
    ("process.prepare_steps", [["python3", "tools/review/anchors.py", "--out", "{job_dir}/anchors.json"]], "job_dir"),
    ("process.run_dir", "tools/review/runs/PR_{pr}_{merge_base}", "merge_base"),
    ("readiness.status_command", ["python3", "tools/threads/threads.py", "status", "{pr}"], "pr"),
    ("readiness.threads_command", ["python3", "tools/threads/threads.py", "threads", "{head9}"], "head9"),
])
def test_reviewer_placeholder_the_runtime_never_fills_there_raises(field, value, placeholder):
    """Validation accepts, per field, exactly what the runtime fills there: anything else loads a config whose
    every job (or every poll) fails."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((field, value)), _vcs(), "r.yaml")
    where = f"reviewer.{field}" + (".0" if field == "process.prepare_steps" else "")
    assert e.value.field == where and str(e.value).startswith(f"{where}: placeholder '{{{placeholder}}}' ")

def test_reviewer_placeholders_the_runtime_fills_load():
    data = _with(("process.worktree_dir", "../wt/PR_{pr}_{head9}"))
    data["reviewer"]["process"]["prepare_steps"] = [
        ["tools/gen.sh", "{pr}", "{worktree}", "{merge_base}", "{head9}", "{repo}"]]
    data["reviewer"]["readiness"]["threads_command"] = ["python3", "tools/threads/threads.py", "{repo}", "{pr}"]
    assert _parse_reviewer_section(data, _vcs(), "r.yaml").enabled

@pytest.mark.parametrize("field, command", [
    ("poster.review_command", ["python3", "{report}"]),
    ("poster.review_command", ["node", "tools/review/runs/PR_{pr}/x.md"]),
    ("poster.replies_command", ["sh", "{plan}"]),
])
def test_reviewer_launcher_running_a_session_file_raises_whatever_its_extension(field, command):
    """An interpreter ignores the extension: the file a launcher runs is never data."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((field, command)), _vcs(), "r.yaml")
    where = f"reviewer.{field}"
    assert e.value.field == where and str(e.value).startswith(f"{where} runs ")


# --- Plan 5: poster placeholders and the {job_dir} guard ---

_POSTER = _section(FULL)["reviewer"]["poster"]

def _drop(command, *args):
    return [arg for arg in command if arg not in args]

@pytest.mark.parametrize("command", [
    ["python3", "{job_dir}/post.py"],
    ["{job_dir}/post"],
    ["python3", "{report}"],
    ["sh", "-c", "python3 {job_dir}/x.py"],
    ["node", "{plan}"],
    ["sh", "-c", "cd {job_dir} && python3 x.py"],
    ["sh", "-c", "PYTHONPATH={job_dir} python3 tools/review/post.py"],
    ["sh", "-c", ". {job_dir}/vars.txt && python3 tools/review/post.py"],
    ["command", "python3", "{report}"],
    ["timeout", "30", "python3", "{job_dir}/x.py"],
    ["sh", "-c", "nice -n 10 python3 {report}"],
])
def test_poster_guard_job_dir(command):
    """The job copy holds what the review session wrote: a poster command running a file from it would run the
    session's code with the forge token."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with(("poster.review_command", command)), _vcs(), "r.yaml")
    where = "reviewer.poster.review_command"
    assert e.value.field == where and str(e.value).startswith(f"{where} runs ")
    assert "from inside {job_dir}, the job's copy of what the review session wrote" in str(e.value)

@pytest.mark.parametrize("field, command, where", [
    ("readiness.threads_command", ["sh", "-c", "cd tools/review/runs/PR_{pr} && python3 x.py"],
     "reviewer.process.run_dir"),
    ("poster.replies_command", ["sh", "-c", "cd {job_dir}; ./post --plan {plan}"], "{job_dir}"),
    ("poster.review_command", ["sh", "-c", "pushd ../review-wt/PR_{pr} && ./gradlew x"],
     "reviewer.process.worktree_dir"),
])
def test_token_command_changing_into_an_untrusted_directory_raises(field, command, where):
    """What a readiness or poster command runs after `cd` runs from there, with the forge token."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((field, command)), _vcs(), "r.yaml")
    assert e.value.field == f"reviewer.{field}"
    assert str(e.value).startswith(f"reviewer.{field} runs {command[-1]!r} from inside {where}, ")

def test_prepare_step_may_change_into_the_worktree():
    """Building the PR in a prepare step is the operator's choice (the example config says so)."""
    step = ["sh", "-c", "cd {worktree} && ./gradlew x"]
    r = _parse_reviewer_section(_with(("process.prepare_steps", [step])), _vcs(), "r.yaml")
    assert r.process.prepare_steps == [step]

@pytest.mark.parametrize("field, command", [
    ("readiness.threads_command",
     ["sh", "-c", ". tools/review/runs/PR_{pr}/vars.txt && python3 tools/threads/threads.py {pr}"]),
    ("readiness.threads_command",
     ["sh", "-c", "PYTHONPATH=tools/review/runs/PR_{pr} python3 tools/threads/threads.py {pr}"]),
    ("process.prepare_steps", [["sh", "-c", "source {worktree}/env.txt; python3 tools/review/anchors.py {pr}"]]),
    ("process.prepare_steps", [["sh", "-c", "PYTHONPATH={worktree} python3 tools/review/anchors.py {pr}"]]),
])
def test_reviewer_command_sourcing_or_importing_untrusted_code_raises(field, command):
    """`.` and `source` run their file whatever its extension; a NAME=value before the program is checked as
    `env NAME=value` is."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((field, command)), _vcs(), "r.yaml")
    where = f"reviewer.{field}" + (".0" if field == "process.prepare_steps" else "")
    assert e.value.field == where and str(e.value).startswith(f"{where} runs ")

@pytest.mark.parametrize("field, command, placeholder", [
    ("poster.review_command", ["python3", "{job_dir}/../other/post.py"], "job_dir"),
    ("poster.review_command", ["python3", "tools/review/post.py", "--anchors", "{job_dir}/../other/anchors.json"],
     "job_dir"),
    ("poster.review_command", ["sh", "-c", "python3 tools/review/post.py --report {report}/../../x.md"], "report"),
    ("poster.replies_command", ["python3", "tools/threads/threads.py", "--plan", "{plan}/../../x.json"], "plan"),
])
def test_poster_path_climbing_out_of_the_job_copy_raises(field, command, placeholder):
    """Next to the job's copy are the other jobs' copies: `..` after it would leave what the guard checks."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((field, command)), _vcs(), "r.yaml")
    where = f"reviewer.{field}"
    assert e.value.field == where
    assert str(e.value).startswith(f"{where} has '..' after {{{placeholder}}} in {command[-1]!r}: ")

def test_poster_guard_allows_data_in_job_dir():
    command = ["python3", "tools/review/post.py", "{pr}", "--report", "{report}", "--anchors", "{job_dir}/anchors.json",
               "--ledger", "{ledger}", "--event", "{event}"]
    r = _parse_reviewer_section(_with(("poster.review_command", command)), _vcs(), "r.yaml")
    assert r.poster.review_command == command

def test_poster_placeholders():
    """The worktree is gone by posting time; the replies ledger is the threads tool's own, next to the plan."""
    takes = {"review_command": "{event}, {head9}, {job_dir}, {ledger}, {plan}, {pr}, {repo}, {report}",
             "replies_command": "{head9}, {job_dir}, {plan}, {pr}, {repo}"}
    for name, placeholder in [("review_command", "worktree"), ("review_command", "merge_base"),
                              ("replies_command", "ledger"), ("replies_command", "report"),
                              ("replies_command", "event")]:
        command = _POSTER[name] + ["--extra", f"{{{placeholder}}}"]
        with pytest.raises(ConfigError) as e:
            _parse_reviewer_section(_with((f"poster.{name}", command)), _vcs(), "r.yaml")
        where = f"reviewer.poster.{name}"
        assert e.value.field == where
        assert str(e.value).startswith(
            f"{where}: placeholder '{{{placeholder}}}' is never filled there; it takes {takes[name]} ")
    command = _POSTER["replies_command"] + ["--head", "{head9}", "--log", "{job_dir}/x.json"]
    r = _parse_reviewer_section(_with(("poster.replies_command", command)), _vcs(), "r.yaml")
    assert r.poster.replies_command == command

@pytest.mark.parametrize("name, command, text", [
    ("review_command", _drop(_POSTER["review_command"], "--ledger", "{ledger}"),
     "reviewer.poster.review_command must pass {ledger}: Cleave fills it on every post"),
    ("review_command", _drop(_POSTER["review_command"], "--report", "{report}"),
     "reviewer.poster.review_command must pass {report}: Cleave fills it on every post"),
    ("review_command", _drop(_POSTER["review_command"], "--event", "{event}"),
     "reviewer.poster.review_command must pass {event}: Cleave fills it on every post"),
    ("replies_command", _drop(_POSTER["replies_command"], "--plan", "{plan}"),
     "reviewer.poster.replies_command must pass {plan}: Cleave fills it on every post"),
    # The first one missing, in the order report, ledger, event.
    ("review_command", _drop(_POSTER["review_command"], "--report", "{report}", "--ledger", "{ledger}"),
     "reviewer.poster.review_command must pass {report}: Cleave fills it on every post"),
    ("review_command", _drop(_POSTER["review_command"], "--ledger", "{ledger}", "--event", "{event}"),
     "reviewer.poster.review_command must pass {ledger}: Cleave fills it on every post"),
    # The untrusted-directory guard comes first.
    ("review_command", ["python3", "{report}"], "reviewer.poster.review_command runs '{report}' "),
], ids=["ledger", "report", "event", "plan", "report-first", "ledger-before-event", "guard-first"])
def test_poster_required_placeholders(name, command, text):
    """Cleave fills these on every post: without {ledger} a post could land twice."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((f"poster.{name}", command)), _vcs(), "r.yaml")
    assert e.value.field == f"reviewer.poster.{name}" and str(e.value).startswith(text)

@pytest.mark.parametrize("name, value, field, text", [
    ("confirm_flag", True, "confirm_flag", "reviewer.poster.confirm_flag must be a non-empty string"),
    ("dry_run_flag", ["--dry-run"], "dry_run_flag", "reviewer.poster.dry_run_flag must be a non-empty string"),
    ("confirm_flag", "--dry-run", "confirm_flag",
     "reviewer.poster.confirm_flag must differ from reviewer.poster.dry_run_flag: "),
    ("review_command", _POSTER["review_command"] + ["--yes"], "review_command",
     "reviewer.poster.review_command passes '--yes' itself: "),
    ("replies_command", ["python3", "tools/threads/threads.py", "post", "{pr}", "--plan", "{plan}", "--dry-run"],
     "replies_command", "reviewer.poster.replies_command passes '--dry-run' itself: "),
], ids=["confirm-not-a-string", "dry-run-not-a-string", "same-flag", "confirm-in-review", "dry-run-in-replies"])
def test_poster_flags(name, value, field, text):
    """Cleave appends one of the two flags to every run: a template passing one itself, or one flag for both,
    would turn a dry run into a post."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((f"poster.{name}", value)), _vcs(), "r.yaml")
    assert e.value.field == f"reviewer.poster.{field}" and str(e.value).startswith(text)

_POST = "python3 tools/review/post.py {pr} --report {report} --ledger {ledger} --event {event}"
_REPLY = "python3 tools/threads/threads.py post {pr} --plan {plan}"

@pytest.mark.parametrize("name, command", [
    ("review_command", ["sh", "-c", _POST]),
    ("replies_command", ["env", "bash", "-ec", _REPLY]),
    ("replies_command", ["env", "-i", "PATH=/usr/bin:/bin", "/bin/dash", "-c", _REPLY]),
    ("review_command", ["nice", "-n", "5", "timeout", "600", "zsh", "-f", "-c", _POST]),
    ("replies_command", ["command", "ksh", "-Cc", _REPLY]),
], ids=["sh-c", "env-bash-ec", "env-options-dash", "wrappers-zsh", "command-ksh-Cc"])
def test_poster_shell_code_raises(name, command):
    """The flag Cleave appends after `sh -c` code becomes the code's $0 and never reaches the tool: a tool that
    posts by default would post on the dry run."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with((f"poster.{name}", command)), _vcs(), "r.yaml")
    where = f"reviewer.poster.{name}"
    assert e.value.field == where
    assert str(e.value) == (f"{where} must be a direct command, not shell -c code: the dry-run flag Cleave "
                            f"appends must reach the tool (file: r.yaml) (field: {where})")

@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "/bin/dash"])
def test_poster_shell_running_a_session_file_raises(shell):
    """Every shell runs its first operand as a script, whatever its extension: from the job's copy that is the
    session's code, run with the forge token."""
    command = [shell, "{job_dir}/notes.md", "--report", "{report}", "--ledger", "{ledger}", "--event", "{event}"]
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with(("poster.review_command", command)), _vcs(), "r.yaml")
    where = "reviewer.poster.review_command"
    assert e.value.field == where
    assert str(e.value).startswith(f"{where} runs '{{job_dir}}/notes.md' from inside {{job_dir}}, the job's copy of "
                                   "what the review session wrote")

def test_poster_script_run_by_a_shell_loads():
    """A shell running the base branch's script passes the flag on: it is the script's argument."""
    command = ["sh", "tools/review/post.sh", "{pr}", "--report", "{report}", "--ledger", "{ledger}", "--event", "{event}"]
    r = _parse_reviewer_section(_with(("poster.review_command", command)), _vcs(), "r.yaml")
    assert r.poster.review_command == command


# --- /code-review fix wave C1: a wrapper's durations and long options ---

# sh -c code that never passes on the flag Cleave appends: it becomes the code's $4.
_SHELL_CODE = ["sh", "-c", 'python3 tools/review/post.py "$1" "$2" "$3"', "sh", "--report={report}",
               "--ledger={ledger}", "--event={event}"]
_DIRECT = "must be a direct command, not shell -c code"
_IN_JOB_DIR = "runs '{job_dir}' from inside {job_dir}"

@pytest.mark.parametrize("command, text", [
    (["timeout", "5s", *_SHELL_CODE], _DIRECT),   # "5s" is a duration, not -s with its value
    (["timeout", "9s", "python3", "{job_dir}", "--report", "{report}", "--ledger", "{ledger}", "--event", "{event}"],
     _IN_JOB_DIR),
    (["timeout", "--signal", "KILL", "30", *_SHELL_CODE], _DIRECT),
    (["timeout", "--signal=KILL", "--kill-after", "5", "1.5m", *_SHELL_CODE], _DIRECT),
    (["nice", "--adjustment", "5", *_SHELL_CODE], _DIRECT),
    (["stdbuf", "--output", "L", *_SHELL_CODE], _DIRECT),
    (["xargs", "--delimiter", ",", *_SHELL_CODE], _DIRECT),
], ids=["timeout-5s-sh", "timeout-9s-job-dir", "signal-kill", "long-forms", "nice-adjustment", "stdbuf-output",
        "xargs-delimiter"])
def test_wrapper_durations_and_long_options_are_skipped(command, text):
    """A number or a duration takes no value, and a long option that takes one is skipped with it: the guard and
    the shell -c check look at the command the wrapper runs."""
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with(("poster.review_command", command)), _vcs(), "r.yaml")
    assert e.value.field == "reviewer.poster.review_command" and text in str(e.value)

def test_a_wrapped_direct_poster_still_loads():
    command = ["timeout", "30", "python3", "tools/post.py", "{report}", "--ledger", "{ledger}", "--event", "{event}"]
    assert _parse_reviewer_section(_with(("poster.review_command", command)), _vcs(), "r.yaml").enabled


# --- /code-review fix wave C11: the flag check compares whole arguments ---

def _with_flag(command, confirm_flag="-y"):
    data = _with(("poster.review_command", command))
    data["reviewer"]["poster"]["confirm_flag"] = confirm_flag
    return data

def test_an_argument_that_only_contains_the_flag_loads():
    command = _POSTER["review_command"] + ["--style=my-yaml"]
    assert _parse_reviewer_section(_with_flag(command), _vcs(), "r.yaml").poster.review_command == command

@pytest.mark.parametrize("command, flag", [
    (_POSTER["review_command"] + ["-y"], "-y"),
    (_POSTER["review_command"] + ["--yes=true"], "--yes"),
    # A shell behind a program the guard does not know: its code's words count one by one.
    (["busybox", "sh", "-c", "python3 tools/review/post.py -y", "sh", "{report}", "{ledger}", "{event}"], "-y"),
], ids=["equal", "with-a-value", "a-word-of-shell-code"])
def test_an_argument_that_is_the_flag_raises(command, flag):
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with_flag(command, flag), _vcs(), "r.yaml")
    assert e.value.field == "reviewer.poster.review_command"
    assert str(e.value).startswith(f"reviewer.poster.review_command passes {flag!r} itself: ")


# --- Address jobs ---

from config.schemas import ProcessPartConfig, ReviewerJobConfig

def test_address_defaults_and_job():
    assert ProcessPartConfig().address_doc == "" and ProcessPartConfig().address_protected == [".github/**"]
    assert ReviewerGlobalConfig().jobs["address"] == ReviewerJobConfig("opus", "xhigh", 5400)

@pytest.mark.parametrize("value", ["/abs/doc.md", "../doc.md"])
def test_address_doc_must_be_repo_relative(value):
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with(("process.address_doc", value)), _vcs(), "r.yaml")
    assert e.value.field == "reviewer.process.address_doc"

def test_address_doc_must_be_a_string():
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with(("process.address_doc", ["a.md"])), _vcs(), "r.yaml")
    assert e.value.field == "reviewer.process.address_doc"

@pytest.mark.parametrize("value", [".github/**", [1], ["ok", None]])
def test_address_protected_must_be_a_list_of_strings(value):
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with(("process.address_protected", value)), _vcs(), "r.yaml")
    assert e.value.field == "reviewer.process.address_protected"

def test_address_settings_load():
    data = _with(("process.address_doc", ".github/pr_comments/400-pr-review-workflow.mdc"))
    data["reviewer"]["process"]["address_protected"] = [".github/**", "gradle/**"]
    process = _parse_reviewer_section(data, _vcs(), "r.yaml").process
    assert process.address_doc == ".github/pr_comments/400-pr-review-workflow.mdc"
    assert process.address_protected == [".github/**", "gradle/**"]


def test_findings_job_defaults():
    assert ReviewerGlobalConfig().jobs["findings"] == ReviewerJobConfig("opus", "xhigh", 5400)


# --- Large PRs: the sharded section ---

def test_sharded_defaults():
    assert ProcessPartConfig().sharded_section == "" and ProcessPartConfig().sharded_min_files == 40

def test_sharded_settings_load():
    data = _with(("process.sharded_section", "Unattended sharded run"))
    data["reviewer"]["process"]["sharded_min_files"] = 60
    process = _parse_reviewer_section(data, _vcs(), "r.yaml").process
    assert (process.sharded_section, process.sharded_min_files) == ("Unattended sharded run", 60)

def test_sharded_section_must_be_a_string():
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with(("process.sharded_section", ["Run"])), _vcs(), "r.yaml")
    assert e.value.field == "reviewer.process.sharded_section"

@pytest.mark.parametrize("value", [0, 40.5, True, "40"])
def test_sharded_min_files_must_be_a_count(value):
    with pytest.raises(ConfigError) as e:
        _parse_reviewer_section(_with(("process.sharded_min_files", value)), _vcs(), "r.yaml")
    assert e.value.field == "reviewer.process.sharded_min_files"
