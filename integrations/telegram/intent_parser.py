"""Intent parser — classifies free-text Telegram messages via Claude CLI."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from orchestrator.tg_i18n import ENGLISH, current_language, tr

logger = logging.getLogger(__name__)

# Added to the prompt when the chat's Telegram language is not English: the reply is shown as it is written.
_REPLY_LANGUAGES = {"uk": "Ukrainian"}
REPLY_LANGUAGE_LINE = (
    'Write the "reply" field in {language}; keep the JSON keys, the intent names and the params exactly as above.'
)

INTENT_SYSTEM_PROMPT = """\
You are the command parser for Cleave, an autonomous dev pipeline.
Current state:
- Mode: {mode}
- Awaiting approval: {awaiting_approval}
- Active workspaces: {active_workspaces}
- Blocked (waiting for human input): {blocked_workspaces}
- Deferred (waiting for Claude quota reset): {deferred_workspaces}

Classify the user message into one of these intents:
  status, analyze, approve, reject, set_mode, retry, provide_input, reviewed, unanswered, unknown

Return ONLY valid JSON (no markdown, no code fences):
{{"intent": "...", "params": {{...}}, "reply": "..."}}

Intent param schemas:
- status: params.ticket_id (optional string) for drill-down
- analyze: params.ticket_ids (required list of strings)
- approve: params.ticket_id (optional string, infer from context if one workspace awaiting)
- reject: params.ticket_id (optional string)
- set_mode: params.mode (required, "auto" or "manual")
- retry: params.ticket_id (required string), params.from_stage (optional: "analysis", "dev", "qa", "push" — defaults to current stage). Use for BLOCKED, FAILED, or DEFERRED tickets. "resume TICKET" and "retry TICKET" both map here.
- provide_input: params.ticket_id (required if multiple blocked, infer from context if exactly one), params.input_text (the user's full answer/clarification verbatim)
- reviewed: params.ticket_id (optional, infer if one PR_REVIEW workspace). User signals code review is done — "reviewed", "review done", "review complete".
- unanswered: params.ticket_id (optional string). Triggered when the user is asking what's still waiting on them in PR review (e.g., `/unanswered`, `/repeat`, `what's pending`, `which comments are open`, `unanswered`).
- unknown: params.raw_text (the original message)

IMPORTANT: If there are blocked workspaces and the user's message looks like an answer/clarification/requirements (not a command), classify as "provide_input". Free-form text like "the bug is X", "we need to scroll Y", "yes, both screens", or descriptions of requirements should be "provide_input" when a workspace is blocked.

The "reply" field is a natural language confirmation message for the user.\
"""


@dataclass
class ParsedIntent:
    """Result of intent classification."""
    intent: str
    params: dict[str, Any] = field(default_factory=dict)
    reply: str = ""

    @staticmethod
    def from_json(raw: str) -> ParsedIntent:
        """Parse a JSON string into a ParsedIntent, with fallback for malformed input."""
        try:
            data = json.loads(raw)
            return ParsedIntent(
                intent=data.get("intent", "unknown"),
                params=data.get("params", {}),
                reply=data.get("reply", ""),
            )
        except (json.JSONDecodeError, TypeError, AttributeError):
            return ParsedIntent(
                intent="unknown",
                params={"raw_text": raw},
                reply=tr("I didn't understand that. I can do: status checks, analyze tickets, approve/reject steps, "
                         "switch modes, unanswered comment recall."),
            )


_STATUS_WORDS = ("status", "статус")
_KEY = re.compile(r"([A-Za-z][A-Za-z0-9_]*-)?[0-9]+")  # a ticket key, or only its number
_STAGE_ALIASES = {"pushed": "push"}  # the stage's state name where the retry stage's name differs


def parse_fixed_intent(text: str) -> ParsedIntent | None:
    """The intent of a command with a fixed shape, read without the LLM: `status`, `status <key>`, `retry <key>`,
    `retry <key> from <stage>` (a leading "/" and a "/command@bot" suffix allowed; the key as typed, a full key or
    its number). None for anything else, which the LLM reads."""
    words = text.strip().rstrip(".!").split()
    if not words:
        return None
    command = words[0].lower().lstrip("/").partition("@")[0]
    rest = words[1:]
    if rest and not _KEY.fullmatch(rest[0]):
        return None
    if command in _STATUS_WORDS and len(rest) <= 1:
        return ParsedIntent("status", {"ticket_id": rest[0]} if rest else {})
    if command != "retry" or not rest:
        return None
    if len(rest) == 1:
        return ParsedIntent("retry", {"ticket_id": rest[0]})
    if rest[1].lower() != "from" or len(rest) < 3:
        return None
    stage = "_".join(rest[2:]).lower()
    return ParsedIntent("retry", {"ticket_id": rest[0], "from_stage": _STAGE_ALIASES.get(stage, stage)})


class IntentParser:
    """Classifies free-text messages into pipeline intents using Claude CLI."""

    def __init__(self, llm_adapter: Any, intent_parser_config: Any | None = None) -> None:
        self._llm = llm_adapter
        self._timeout = 5
        if intent_parser_config and hasattr(intent_parser_config, "timeout_seconds"):
            self._timeout = intent_parser_config.timeout_seconds

    async def parse(self, message: str, pipeline_context: dict[str, Any]) -> ParsedIntent:
        """Classify a user message into an intent.

        Args:
            message: Raw text from Telegram.
            pipeline_context: Dict with keys: mode, awaiting_approval, active_workspaces.

        Returns:
            ParsedIntent with intent, params, and reply.
        """
        system = INTENT_SYSTEM_PROMPT.format(
            mode=pipeline_context.get("mode", "auto"),
            awaiting_approval=", ".join(pipeline_context.get("awaiting_approval", [])) or "none",
            active_workspaces=", ".join(pipeline_context.get("active_workspaces", [])) or "none",
            blocked_workspaces=", ".join(pipeline_context.get("blocked_workspaces", [])) or "none",
            deferred_workspaces=", ".join(pipeline_context.get("deferred_workspaces", [])) or "none",
        )
        language = current_language()
        if language != ENGLISH and language in _REPLY_LANGUAGES:
            system += "\n" + REPLY_LANGUAGE_LINE.format(language=_REPLY_LANGUAGES[language])

        try:
            raw = await self._llm.quick_query(
                prompt=message,
                system=system,
                timeout=self._timeout,
            )
            return ParsedIntent.from_json(raw)
        except Exception as e:
            logger.error("Intent parsing failed: %s", e)
            return ParsedIntent(
                intent="error",
                params={},
                reply=tr("I'm having trouble understanding right now. Try again in a moment."),
            )
