"""The LLM fallback for a reply the grammar does not know: one scrubbed `claude -p` turn, without tools, that
turns it into a command. The model sees only the reply and the review's numbers with their kinds; its answer is
untrusted and checked field by field. Nothing runs from it until the operator taps Yes on its echo.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from config.schemas import ReviewerJobConfig
from orchestrator.reviewer.grammar import ReplyCommand
from orchestrator.reviewer.markdown import strip_fences
from orchestrator.reviewer.poster_output import strict_loads
from orchestrator.reviewer.report import Report
from orchestrator.reviewer.session import session_env

logger = logging.getLogger(__name__)

REPLY_SYSTEM_PROMPT = """\
You turn one operator reply to a Cleave code-review message into one command. You see only the reply and the review's numbers with their kinds.
The review's numbers: {numbers}
Commands:
- post: publish findings by number. numbers: the numbers to publish; event: "APPROVE", "REQUEST_CHANGES" or "COMMENT" (default "COMMENT"); no_summary: true when the reply asks to leave the summary out; anyway: true when it asks to post although a review was already submitted.
- post_replies: publish drafted re-check replies by number.
- text: show the text of the numbers.
- edit: rewrite one number's text. numbers: [that number]; instruction: what to change, in the operator's words.
- draft: write a comment for one dropped number.
- review_again: review the PR again.
- skip: skip this head.
- none: anything else (a question, a remark, an unclear reply).
"Confirmed" means critical, major and minor. Turn phrases such as "all but 3" or "1 to 4" into explicit numbers, using only the numbers listed above.
Return ONLY valid JSON (no markdown, no code fences):
{"action": "...", "numbers": [], "event": "COMMENT", "no_summary": false, "anyway": false, "instruction": ""}"""

_KINDS = {"red": "critical", "orange": "major", "yellow": "minor", "question": "question",
          "preexisting": "pre-existing"}
_ACTIONS = ("post", "post_replies", "text", "edit", "draft", "review_again", "skip")
_EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")
# Every field the answer may hold, with its type: any other field means not understood.
_FIELDS = {"action": str, "numbers": list, "event": str, "no_summary": bool, "anyway": bool, "instruction": str}
_MAX_NUMBER = 999_999_999  # the most a report number can be: nine digits


def reply_context(kind: str, report: Report | None, replies: list[dict], drafted: set[int]) -> str:
    """The numbers the reply may name, with their kinds, in report order: never a title or any other text the
    review model wrote. A drafted dropped number is listed once, as dropped."""
    if kind == "recheck":
        numbers = ", ".join(str(entry["n"]) for entry in replies)
        return f"replies {numbers}" if numbers else "none"
    if report is None:
        return "none"
    listed_as_dropped = drafted & {d.number for d in report.dropped}
    parts = [f"{i.number} {_KINDS[i.kind]}" for i in report.items if i.number not in listed_as_dropped]
    parts += [
        f"{d.number} dropped, drafted as a question" if d.number in drafted else f"{d.number} dropped"
        for d in report.dropped
    ]
    return ", ".join(parts) or "none"


def command_from_json(raw: str) -> ReplyCommand | None:
    """The command in the model's answer; None for "none" and for anything not exactly of the answer's shape (a
    key given twice included). A field the action does not take is reset, so the command confirmed is exactly the
    one describe() echoes."""
    try:
        data = strict_loads(strip_fences(raw))
    except (ValueError, RecursionError):
        return None
    # type(), not isinstance(): a bool is no int and 1 no bool.
    if not (isinstance(data, dict) and data.keys() <= _FIELDS.keys()
            and all(type(value) is _FIELDS[name] for name, value in data.items())):
        return None
    numbers = data.get("numbers", [])
    event = data.get("event", "COMMENT")
    if data.get("action") not in _ACTIONS or event not in _EVENTS:  # "none" too
        return None
    if not all(type(n) is int and 0 <= n <= _MAX_NUMBER for n in numbers):
        return None
    action = data["action"]
    post = action == "post"
    return ReplyCommand(
        action, () if action in ("review_again", "skip") else tuple(numbers), event if post else "COMMENT",
        no_summary=post and data.get("no_summary", False),
        anyway=post and data.get("anyway", False),
        instruction=data.get("instruction", "") if action == "edit" else "",
    )


class ReplyParser:
    """Asks the model, in one turn without tools in a 0700 directory of its own and with the session's
    scrubbed environment, which command a reply means."""

    def __init__(self, llm, job_cfg: ReviewerJobConfig, *, work_dir: Path,
                 environ: Mapping[str, str] = os.environ) -> None:
        self._llm = llm
        self._job_cfg = job_cfg
        self._work_dir = work_dir
        self._environ = environ

    async def parse(self, message: str, context: str) -> ReplyCommand | None:
        """The command `message` means, given `context` (reply_context); None when not understood or the run
        fails."""
        system = REPLY_SYSTEM_PROMPT.replace("{numbers}", context)
        try:
            self._work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._work_dir.chmod(0o700)
            response = await self._llm.execute_in_workspace(
                prompt=f"{system}\n\n---\n\n{message}",
                cwd=str(self._work_dir),
                model=self._job_cfg.model,
                timeout=self._job_cfg.timeout_seconds,
                max_turns=1,
                env=session_env(self._environ),
                extra_args=["--restricted", "--tools", "", "--strict-mcp-config"],
            )
            return command_from_json(response.content)
        except Exception as e:
            logger.warning("Reviewer reply not parsed: %s", type(e).__name__)
            return None
