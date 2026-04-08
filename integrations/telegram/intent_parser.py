"""Intent parser — classifies free-text Telegram messages via Claude CLI."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

INTENT_SYSTEM_PROMPT = """\
You are the command parser for Sickle, an autonomous dev pipeline.
Current state:
- Mode: {mode}
- Awaiting approval: {awaiting_approval}
- Active workspaces: {active_workspaces}

Classify the user message into one of these intents:
  status, analyze, approve, reject, set_mode, unknown

Return ONLY valid JSON (no markdown, no code fences):
{{"intent": "...", "params": {{...}}, "reply": "..."}}

Intent param schemas:
- status: params.ticket_id (optional string) for drill-down
- analyze: params.ticket_ids (required list of strings)
- approve: params.ticket_id (optional string, infer from context if one workspace awaiting)
- reject: params.ticket_id (optional string)
- set_mode: params.mode (required, "auto" or "manual")
- unknown: params.raw_text (the original message)

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
                reply="I didn't understand that. I can do: status checks, analyze tickets, approve/reject steps, switch modes.",
            )


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
        )

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
                reply="I'm having trouble understanding right now. Try again in a moment.",
            )
