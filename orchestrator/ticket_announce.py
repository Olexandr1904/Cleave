"""Tells the operator's Telegram chat what the dashboard just did to a ticket."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from orchestrator import tg_format
from orchestrator.tg_i18n import telegram_language, tr

logger = logging.getLogger(__name__)


class TicketAnnouncer:
    """Sends one line per dashboard action to the ticket's chat, in the project's Telegram language."""

    def __init__(self, notifier: Any, chat_id_of: Callable[[Any], str], language_of: Callable[[str], str]) -> None:
        self._notifier = notifier
        self._chat_id_of = chat_id_of
        self._language_of = language_of

    async def announce(self, ws: Any, what: Callable[[], str], *, reply_to: int | None = None) -> None:
        """Send "🖥 Dashboard: <what>": a reply to `reply_to`, else under the ticket's header. `what` runs in the
        project's language. Never raises: a failure is logged."""
        try:
            with telegram_language(self._language_of(ws.state.company_id)):
                line = tr("Dashboard: {what}", what=what())
                if reply_to is None:
                    header = tg_format.tg_header("🖥", ws.state.company_id, ws.state.ticket_id,
                                                 tg_format.read_ticket_title(ws))
                    text = f"{header}\n{line}"
                else:
                    text = f"🖥 {line}"
            await self._notifier.send_message(self._chat_id_of(ws), text, reply_to_message_id=reply_to)
        except Exception:
            logger.warning("Could not announce a dashboard action on %s in Telegram",
                           getattr(getattr(ws, "state", None), "ticket_id", "?"), exc_info=True)
