# tests/unit/test_reviewer_reply_llm.py
from __future__ import annotations

import inspect
import stat
from pathlib import Path

import pytest

from config.schemas import ReviewerJobConfig
from integrations.llm.llm_interface import LLMResponse
from orchestrator.reviewer import reply_llm
from orchestrator.reviewer.grammar import ReplyCommand
from orchestrator.reviewer.markdown import strip_fences
from orchestrator.reviewer.reply_llm import ReplyParser, command_from_json, reply_context
from orchestrator.reviewer.report import parse_report
from orchestrator.reviewer.report_edit import apply_draft
from tests.unit.test_reviewer_report import HEAD, REPORT

FULL = "1 critical, 2 major, 3 minor, 4 question, 5 question, 6 pre-existing, 7 dropped, 8 dropped, 9 dropped"
DRAFTED_7 = FULL.replace("7 dropped", "7 dropped, drafted as a question")


class FakeLLM:
    """Records execute_in_workspace kwargs; returns `content` or raises `error`."""

    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[dict] = []

    async def execute_in_workspace(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return LLMResponse(content=self.content, input_tokens=0, output_tokens=0)


def test_reply_context():
    report = parse_report(REPORT, HEAD)
    assert reply_context("review", report, [], set()) == FULL
    assert reply_context("review", report, [], {7}) == DRAFTED_7
    drafted = parse_report(
        apply_draft(REPORT, HEAD, 7, "Null check", "app/src/main/Adapter.kt", 12, "Is this reachable?",
                    frozenset()),
        HEAD, frozenset({7}),
    )
    assert [i.number for i in drafted.items if i.kind == "question"] == [4, 5, 7]
    assert reply_context("review", drafted, [], {7}) == DRAFTED_7
    assert "NPE" not in reply_context("review", report, [], set())
    replies = [{"n": 1, "body": "Fixed, thanks."}, {"n": 3, "body": "Still there."}]
    assert reply_context("recheck", None, replies, set()) == "replies 1, 3"


def test_reply_context_without_numbers():
    assert reply_context("review", None, [], set()) == "none"
    assert reply_context("recheck", None, [], set()) == "none"


def test_strip_fences():
    assert strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_fences("```\nx\n```\nafter") == "x"
    assert strip_fences("  plain  ") == "plain"


async def test_parse_runs_one_scrubbed_turn(tmp_path: Path):
    work_dir = tmp_path / "command"
    work_dir.mkdir()
    work_dir.chmod(0o755)
    llm = FakeLLM('{"action": "post", "numbers": [1, 2, 4, 5, 6], "event": "APPROVE"}')
    environ = {
        "HOME": "/home/op", "PATH": "/usr/bin", "GH_TOKEN": "ghp_not_real",
        "TELEGRAM_BOT_TOKEN": "123:not_real", "ANTHROPIC_API_KEY": "sk-not-real", "CLAUDE_API_KEY": "not_real",
    }
    parser = ReplyParser(llm, ReviewerJobConfig("sonnet", "", 60), work_dir=work_dir, environ=environ)
    message = "post everything except 3, approve"

    cmd = await parser.parse(message, FULL)

    assert cmd == ReplyCommand("post", (1, 2, 4, 5, 6), "APPROVE")
    [call] = llm.calls
    assert call["max_turns"] == 1
    # No --mcp-config: with --strict-mcp-config the turn loads no MCP server of the host's, as a small job's.
    assert call["extra_args"] == ["--restricted", "--tools", "", "--strict-mcp-config"]
    assert call["cwd"] == str(work_dir)
    assert call["model"] == "sonnet"
    assert call["timeout"] == 60
    for key in ("GH_TOKEN", "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        assert key not in call["env"]
    assert call["env"]["HOME"] == "/home/op"
    assert FULL in call["prompt"]
    assert call["prompt"].endswith(f"\n\n---\n\n{message}")
    assert stat.S_IMODE(work_dir.stat().st_mode) == 0o700


def test_command_from_json():
    raw = '```json\n{"action": "edit", "numbers": [2], "instruction": "make it shorter"}\n```'
    assert command_from_json(raw) == ReplyCommand("edit", (2,), instruction="make it shorter")
    for raw in (
        '{"action": "none"}', '{"action": "merge"}', '{"action": "post", "numbers": [true]}',
        '{"action": "post", "event": "LGTM"}', "[]", "nope",
    ):
        assert command_from_json(raw) is None, raw


def test_command_from_json_defaults():
    assert command_from_json('{"action": "review_again"}') == ReplyCommand("review_again")
    full = ('{"action": "post", "numbers": [3, 1], "event": "REQUEST_CHANGES", "no_summary": true, '
            '"anyway": true, "instruction": ""}')
    assert command_from_json(full) == ReplyCommand("post", (1, 3), "REQUEST_CHANGES", no_summary=True,
                                                   anyway=True)


@pytest.mark.parametrize("raw", [
    '{"action": "post", "numbers": [1], "action": "skip"}',
    '{"action": "post", "numbers": [1], "numbers": [1, 2]}',
    '{"action": "post", "numbers": [1], "event": "COMMENT", "event": "APPROVE"}',
])
def test_command_from_json_rejects_duplicate_keys(raw):
    assert command_from_json(raw) is None


@pytest.mark.parametrize(("raw", "cmd"), [
    (('{"action": "post_replies", "numbers": [3, 1], "event": "APPROVE", "no_summary": true, "anyway": true, '
      '"instruction": "be nice"}'), ReplyCommand("post_replies", (1, 3))),
    ('{"action": "review_again", "numbers": [3], "event": "APPROVE"}', ReplyCommand("review_again")),
    ('{"action": "skip", "numbers": [1, 2], "anyway": true}', ReplyCommand("skip")),
    ('{"action": "text", "numbers": [2], "event": "REQUEST_CHANGES", "instruction": "x"}',
     ReplyCommand("text", (2,))),
    ('{"action": "draft", "numbers": [7], "no_summary": true}', ReplyCommand("draft", (7,))),
    ('{"action": "edit", "numbers": [2], "instruction": "shorter", "event": "APPROVE", "anyway": true}',
     ReplyCommand("edit", (2,), instruction="shorter")),
    ('{"action": "post", "numbers": [1], "event": "APPROVE", "instruction": "and approve"}',
     ReplyCommand("post", (1,), "APPROVE")),
])
def test_command_from_json_resets_fields_the_action_does_not_take(raw, cmd):
    """The confirmed command is exactly the echoed one: describe() shows only what the action takes."""
    assert command_from_json(raw) == cmd


@pytest.mark.parametrize("raw", [
    '{"action": "post", "numbers": [1], "all_confirmed": true}',  # a field outside the shape
    '{"action": "post", "numbers": "1"}',
    '{"action": "post", "numbers": [1.0]}',
    '{"action": "post", "numbers": [-1]}',
    '{"action": "post", "numbers": [1000000000]}',
    '{"action": "post", "numbers": [1], "no_summary": "yes"}',
    '{"action": "post", "numbers": [1], "anyway": 1}',
    '{"action": "edit", "numbers": [2], "instruction": 5}',
    '{"action": "post", "numbers": [1], "event": null}',
    '{"action": ["post"]}',
    '{"numbers": [1]}',
    '"post 1"',
    '[' * 100_000,
])
def test_command_from_json_rejects_other_shapes(raw):
    assert command_from_json(raw) is None


async def test_parse_failure_is_none(tmp_path: Path):
    llm = FakeLLM(error=RuntimeError("claude exited 1"))
    parser = ReplyParser(llm, ReviewerJobConfig("sonnet", "", 60), work_dir=tmp_path / "command", environ={})
    assert await parser.parse("hmm, maybe post the first one?", FULL) is None
    assert len(llm.calls) == 1


async def test_parse_not_understood_is_none(tmp_path: Path):
    llm = FakeLLM("I think you mean post 1.")
    parser = ReplyParser(llm, ReviewerJobConfig("sonnet", "", 60), work_dir=tmp_path / "command", environ={})
    assert await parser.parse("what is 4 about?", FULL) is None


def test_no_ticket_intent_parser():
    assert "intent_parser" not in inspect.getsource(reply_llm)
    for path in Path(reply_llm.__file__).parent.glob("*.py"):
        assert "IntentParser" not in path.read_text(encoding="utf-8"), path.name
