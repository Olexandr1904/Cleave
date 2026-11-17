# tests/unit/test_skillflow_session.py
from __future__ import annotations
import json
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
import pytest
from config.schemas import SkillFlowConfig
from integrations.llm.claude_code_adapter import CLIError, QuotaExhaustedError
from integrations.llm.llm_interface import LLMResponse
from orchestrator.skillflow.session import (
    SKILL_MAX_TURNS, SKILL_TOOLS, MissingBuildVariable, run_turn, skill_env, skill_settings_json,
)

RESET = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
Response = partial(LLMResponse, input_tokens=0, output_tokens=0)


class FakeLLM:
    """Records execute_in_workspace kwargs; returns or raises the next item."""

    def __init__(self, items: list) -> None:
        self.items = list(items)
        self.calls: list[dict] = []

    async def execute_in_workspace(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def turn(llm, tmp_path, resume):
    return await run_turn(
        llm, checkout_dir=tmp_path / "checkout", session_dir=tmp_path / "session", prompt="the prompt",
        session_id="sid", resume=resume, model="claude-opus-5-5", cfg=SkillFlowConfig(effort="xhigh"),
        secret_paths=[], env={"HOME": "/h"}, turn=1,
    )


def test_env_is_scrubbed_and_carries_the_build(monkeypatch):
    environ = {"HOME": "/h", "PATH": "/bin", "JAVA_HOME": "/jdk", "GITHUB_TOKEN": "t", "TELEGRAM_BOT_TOKEN": "t",
               "ANTHROPIC_API_KEY": "k", "JIRA_API_TOKEN": "t"}
    env = skill_env(environ, ["JAVA_HOME"], "Cleave Bot", "cleave@pipeline.local")
    assert env["JAVA_HOME"] == "/jdk" and env["HOME"] == "/h"
    assert not {"GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "JIRA_API_TOKEN"} & env.keys()
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert (env["GIT_AUTHOR_NAME"], env["GIT_COMMITTER_EMAIL"]) == ("Cleave Bot", "cleave@pipeline.local")
    assert env["BASH_MAX_TIMEOUT_MS"] == "1500000" and env["BASH_DEFAULT_TIMEOUT_MS"] == "900000"


def test_env_names_a_missing_build_variable():
    with pytest.raises(MissingBuildVariable, match="ANDROID_HOME"):
        skill_env({"HOME": "/h"}, ["ANDROID_HOME"], "a", "b")


def test_settings_deny_the_network_and_the_secrets(tmp_path):
    (tmp_path / ".env").write_text("X=1")
    deny = skill_settings_json([tmp_path / ".env"])["permissions"]["deny"]
    assert {"Bash(gh:*)", "Bash(git push:*)", "Bash(git config:*)", "Bash(curl:*)", "Bash(security:*)"} <= set(deny)
    assert any(rule.startswith("Read(") and rule.endswith("/.env)") for rule in deny)
    assert "sandbox" not in skill_settings_json([])


async def test_first_turn_starts_a_session(tmp_path):
    llm = FakeLLM([Response("ACME-1 | fixed | needs user: no", cost_usd=1.5, num_turns=40)])
    out = await turn(llm, tmp_path, resume=False)
    call = llm.calls[0]
    assert (out.kind, out.text, out.cost_usd, out.turns) == ("ok", "ACME-1 | fixed | needs user: no", 1.5, 40)
    assert call["session_id"] == "sid" and call["resume"] is False and call["resume_fallback"] is False
    assert call["cwd"] == str(tmp_path / "checkout") and call["max_turns"] == SKILL_MAX_TURNS
    assert call["raw_allowed_tools"] == list(SKILL_TOOLS) and "allowed_tools" not in call
    assert "--strict-mcp-config" in call["extra_args"] and "--restricted" not in call["extra_args"]
    settings = Path(call["extra_args"][call["extra_args"].index("--settings") + 1])
    assert json.loads(settings.read_text())["permissions"]["deny"]
    assert "HARD SAFETY RULES" in call["system"] and call["timeout"] == 14400 and call["effort"] == "xhigh"
    assert call["raw_stream_path"].name == "stream-1.jsonl"


async def test_an_ok_turn_carries_its_tokens(tmp_path):
    out = await turn(FakeLLM([Response("done", input_tokens=12000, output_tokens=3400)]), tmp_path, resume=False)
    assert (out.tokens_in, out.tokens_out) == (12000, 3400)
    failed = await turn(FakeLLM([RuntimeError("Claude Code CLI timed out after 14400s")]), tmp_path, resume=False)
    assert (failed.tokens_in, failed.tokens_out) == (0, 0)


async def test_resume_turn(tmp_path):
    llm = FakeLLM([Response("done")])
    await turn(llm, tmp_path, resume=True)
    assert llm.calls[0]["resume"] is True


@pytest.mark.parametrize("error, kind", [
    (QuotaExhaustedError("limit", retry_at=RESET), "quota"),
    (CLIError("x", result_error="Not logged in · Please run /login"), "login"),
    (RuntimeError("Claude Code CLI timed out after 14400s"), "failed"),
])
async def test_errors_are_classified(tmp_path, error, kind):
    out = await turn(FakeLLM([error]), tmp_path, resume=False)
    assert out.kind == kind and (kind != "quota" or out.retry_at == RESET)


async def test_a_missing_session_is_read_from_the_whole_error_text(tmp_path):
    error = CLIError("Claude Code returned error: " + "x" * 400 + " No conversation found with session ID: sid")
    out = await turn(FakeLLM([error]), tmp_path, resume=True)
    assert (out.kind, out.no_session) == ("failed", True) and "conversation" not in out.error.casefold()
    other = await turn(FakeLLM([RuntimeError("Claude Code CLI timed out after 14400s")]), tmp_path, resume=True)
    assert other.no_session is False


async def test_a_missing_session_is_read_from_the_clis_own_error_text(tmp_path):
    error = CLIError("x", result_error="No conversation found with session ID: sid")
    out = await turn(FakeLLM([error]), tmp_path, resume=True)
    assert (out.kind, out.no_session) == ("failed", True)
