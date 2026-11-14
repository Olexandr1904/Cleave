# tests/unit/test_reviewer_readiness.py
from __future__ import annotations
import json, re, stat, subprocess, time
import pytest
from config.schemas import ReadinessPartConfig
from orchestrator.reviewer.commands import CommandResult, expand, part_env, redact, run_command
from orchestrator.reviewer.readiness import (CommandReadiness, ReadinessError, parse_board_snapshot,
                                             parse_threads_snapshot)

SNAP = {"me": "octo", "prs": [{"number": 1712, "title": "Fix cart sync", "url": "u", "author": "octo-author",
        "draft": False, "threads_truncated": False, "checks": "SUCCESS",
        "turns": [{"login": "octo", "action": "review", "threads": []},
                  {"login": "octo-author", "action": "address_review", "threads": [], "reviewer": "octo"}]}]}

def script(tmp_path, body):
    p = tmp_path / "tool.sh"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)

def test_parse_board_snapshot():
    s = parse_board_snapshot(json.dumps(SNAP))
    assert s.me == "octo" and s.prs[0].number == 1712 and s.prs[0].extra == {"checks": "SUCCESS"}
    assert [(t.login, t.action, t.reviewer) for t in s.prs[0].turns] == [("octo", "review", ""),
                                                                         ("octo-author", "address_review", "octo")]

@pytest.mark.parametrize("bad,where", [
    ("not json", "board snapshot"), ({"prs": []}, "me"), ({"me": "o", "prs": [{"number": "1"}]}, "prs[0].number"),
    ({"me": "o", "prs": [{**SNAP["prs"][0], "turns": [{"login": "o"}]}]}, "prs[0].turns[0].action"),
])
def test_parse_board_snapshot_rejects(bad, where):
    with pytest.raises(ReadinessError, match=where.replace("[", r"\[").replace("]", r"\]")):
        parse_board_snapshot(bad if isinstance(bad, str) else json.dumps(bad))

def test_parse_threads_snapshot():
    t = parse_threads_snapshot(json.dumps({"number": 1, "author": "a", "head_sha": "c" * 40, "threads": [],
                                           "review_bodies": []}))
    assert t["head_sha"] == "c" * 40
    with pytest.raises(ReadinessError):
        parse_threads_snapshot(json.dumps({"number": 1, "author": "a", "head_sha": "xyz", "threads": [],
                                           "review_bodies": []}))

def test_expand_and_env_and_redact(monkeypatch):
    assert expand(["t.py", "--repo", "{repo}", "{pr}"], {"repo": "o/r", "pr": "5"}) == ["t.py", "--repo", "o/r", "5"]
    with pytest.raises(ValueError, match="pr"):
        expand(["{pr}"], {"repo": "o/r"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    env = part_env("tok")
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "tok" and "ANTHROPIC_API_KEY" not in env
    assert redact("auth tok failed for tok", ["tok", ""]) == "auth *** failed for ***"

def test_part_env_runs_no_planted_hook_or_fsmonitor(tmp_path, monkeypatch):
    """A part command carries the token: git config planted in the clone or the host's never runs a program
    in its git calls."""
    (tmp_path / ".gitconfig").write_text("[core]\n\taskPass = /usr/bin/false\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, path in (("hook", repo / ".git" / "hooks" / "pre-commit"), ("fsmonitor", tmp_path / "fsmonitor.sh")):
        path.write_text(f"#!/bin/sh\ntouch {tmp_path / name}-ran\n")
        path.chmod(0o755)
    with (repo / ".git" / "config").open("a") as f:
        f.write(f"[core]\n\tfsmonitor = {tmp_path / 'fsmonitor.sh'}\n")
    env = part_env("tok")
    assert (env["GIT_CONFIG_NOSYSTEM"], env["GIT_CONFIG_GLOBAL"]) == ("1", "/dev/null")
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    assert git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x").returncode == 0
    git("status")
    assert not (tmp_path / "hook-ran").exists() and not (tmp_path / "fsmonitor-ran").exists()
    assert git("config", "--get", "core.askpass").stdout == ""   # the host's global config is not read

async def test_board_runs_status_command(tmp_path):
    tool = script(tmp_path, f'[ "$1 $2" = "--repo acme-org/acme-app" ] || exit 9\n'
                            f'[ -n "$GH_TOKEN" ] || exit 8\necho \'{json.dumps(SNAP)}\'\n')
    r = CommandReadiness(ReadinessPartConfig(status_command=[tool, "--repo", "{repo}"], threads_command=[tool]),
                         repo="acme-org/acme-app", token="tok")
    assert (await r.board(tmp_path)).me == "octo"

async def test_readiness_failure_reason_is_redacted(tmp_path):
    tool = script(tmp_path, 'echo "fatal: https://x:SECRET@github.com denied" >&2\nexit 3\n')
    r = CommandReadiness(ReadinessPartConfig(status_command=[tool], threads_command=[tool]), repo="o/r", token="SECRET")
    with pytest.raises(ReadinessError) as e:
        await r.board(tmp_path)
    assert str(e.value).startswith("exit 3: ") and "SECRET" not in str(e.value)

async def test_readiness_timeout(tmp_path):
    tool = script(tmp_path, "sleep 30\n")
    r = CommandReadiness(ReadinessPartConfig(status_command=[tool], threads_command=[tool]), repo="o/r", token="t",
                         timeout=0.5)
    t0 = time.monotonic()
    with pytest.raises(ReadinessError, match="timed out after 0 s"):
        await r.board(tmp_path)
    assert time.monotonic() - t0 < 5

async def test_run_command_missing_executable(tmp_path):
    res = await run_command([str(tmp_path / "nope")], cwd=tmp_path, env={}, timeout=5)
    assert (res.returncode, res.stderr) == (127, f"not found: {tmp_path / 'nope'}")

def test_command_readiness_exposes_repo_not_token():
    r = CommandReadiness(ReadinessPartConfig(), repo="acme-org/acme-app", token="SECRET")
    assert r.repo == "acme-org/acme-app" and "SECRET" not in repr(r)

@pytest.mark.parametrize("arg", ["{pr}", "{repo.owner}", "{repo[x]}"])
async def test_bad_template_is_a_readiness_error(tmp_path, arg):
    tool = script(tmp_path, "exit 0\n")
    r = CommandReadiness(ReadinessPartConfig(status_command=[tool, arg], threads_command=[tool]), repo="o/r", token="t")
    with pytest.raises(ReadinessError, match="bad command template: "):
        await r.board(tmp_path)

@pytest.mark.parametrize("body,mode", [("#!/bin/sh\necho '{}'\n", 0o644), ("echo '{}'\n", 0o755)])
async def test_tool_that_cannot_run(tmp_path, body, mode):
    p = tmp_path / "tool.sh"
    p.write_text(body)
    p.chmod(mode)
    r = CommandReadiness(ReadinessPartConfig(status_command=[str(p)], threads_command=[str(p)]), repo="o/r",
                         token="SECRET")
    with pytest.raises(ReadinessError, match=f"exit 126: cannot run: {re.escape(str(p))}: "):
        await r.board(tmp_path)

def test_parse_board_snapshot_rejects_deep_nesting():
    with pytest.raises(ReadinessError, match="board snapshot: output: "):
        parse_board_snapshot("[" * 100000)  # deep enough for RecursionError, not JSONDecodeError

async def test_failure_reason_redacts_before_cutting(tmp_path):
    tool = script(tmp_path, f'echo "{"x" * 195}SECRET" >&2\nexit 3\n')
    r = CommandReadiness(ReadinessPartConfig(status_command=[tool], threads_command=[tool]), repo="o/r", token="SECRET")
    with pytest.raises(ReadinessError) as e:
        await r.board(tmp_path)
    assert str(e.value) == "exit 3: " + "x" * 195 + "***" and "SECRE" not in str(e.value)


# --- Code-review fix wave 1 ---

async def test_readiness_fills_every_placeholder_its_commands_may_use(tmp_path, monkeypatch):
    """Config validation accepts exactly these sets in the two commands: CommandReadiness fills each."""
    from config.schemas import STATUS_COMMAND_PLACEHOLDERS, THREADS_COMMAND_PLACEHOLDERS
    threads = {"number": 7, "author": "a", "head_sha": "c" * 40, "threads": [], "review_bodies": []}
    calls = []
    async def ran(argv, *, cwd, env, timeout):
        calls.append(argv)
        return CommandResult(0, json.dumps(SNAP if len(calls) == 1 else threads), "", False)
    monkeypatch.setattr("orchestrator.reviewer.readiness.run_command", ran)
    def command(names):
        return ["tool", *(f"{{{n}}}" for n in sorted(names))]
    r = CommandReadiness(ReadinessPartConfig(status_command=command(STATUS_COMMAND_PLACEHOLDERS),
                                             threads_command=command(THREADS_COMMAND_PLACEHOLDERS)),
                         repo="o/r", token="t")
    await r.board(tmp_path)
    await r.threads(tmp_path, 7)
    assert [len(argv) - 1 for argv in calls] == [len(STATUS_COMMAND_PLACEHOLDERS), len(THREADS_COMMAND_PLACEHOLDERS)]
    assert all("{" not in arg for argv in calls for arg in argv)
