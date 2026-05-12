"""Trello tracker adapter — implements TrackerInterface against Trello REST v1."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from integrations.base.tracker import (
    StatusChange,
    TicketComment,
    TicketData,
    TrackerInterface,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]
TIMEOUT = 30
MAX_RETRY_AFTER = 30

API_BASE = "https://api.trello.com/1"


def _is_trello_host(host: str) -> bool:
    """Match trello.com and *.trello.com / atlassian.com and *.atlassian.com.
    Rejects names like nottrello.com that would slip past endswith()."""
    return (
        host == "trello.com" or host.endswith(".trello.com")
        or host == "atlassian.com" or host.endswith(".atlassian.com")
    )


def _card_created_at(card_id: str) -> str:
    """Decode the first 8 hex chars of a Trello card ID as unix timestamp.
    Returns ISO 8601 ('YYYY-MM-DDTHH:MM:SS+00:00')."""
    try:
        ts = int(card_id[:8], 16)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, IndexError):
        return ""


class TrelloAdapter(TrackerInterface):
    """Trello REST v1 adapter. Auth via key+token query params on every request.

    Attachment downloads use OAuth1-style headers; see download_attachment.
    """

    def __init__(
        self,
        *,
        api_key: str,
        token: str,
        board_id: str,
        trigger_labels: list[str] | None = None,
        ignore_labels: list[str] | None = None,
        list_mapping: dict[str, str] | None = None,
    ) -> None:
        self._key = api_key
        self._token = token
        self._board_id = board_id
        self._trigger_labels = trigger_labels or ["ai-pipeline"]
        self._ignore_labels = ignore_labels or []
        self._status_to_list_id: dict[str, str] = {
            k: v for k, v in (list_mapping or {}).items() if v
        }
        self._list_id_to_status: dict[str, str] = {
            v: k for k, v in self._status_to_list_id.items()
        }
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=TIMEOUT,
            params={"key": self._key, "token": self._token},
        )
        self._raw_client = httpx.AsyncClient(timeout=TIMEOUT)
        self._list_id_to_name: dict[str, str] = {}

    async def close(self) -> None:
        await self._client.aclose()
        await self._raw_client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """HTTP request with retries that honor Retry-After on 429."""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.request(method, path, **kwargs)
                if response.status_code == 429:
                    raw = response.headers.get("Retry-After", "1")
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        retry_after = 1.0
                    delay = min(retry_after, MAX_RETRY_AFTER)
                    logger.warning(
                        "Trello rate limit on %s %s; sleeping %.1fs",
                        method, path, delay,
                    )
                    last_error = httpx.HTTPStatusError(
                        f"Rate limited after {attempt + 1} attempts",
                        request=response.request, response=response,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status_code in (401, 403):
                    raise httpx.HTTPStatusError(
                        f"Auth failed: {response.status_code} {response.text[:200]}",
                        request=response.request, response=response,
                    )
                response.raise_for_status()
                if response.status_code == 204:
                    return {}
                if not response.content:
                    return {}
                return response.json()
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_error = e
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                    raise
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    logger.warning(
                        "Trello request retry %d/%d for %s %s: %s",
                        attempt + 1, MAX_RETRIES, method, path, e,
                    )
        raise last_error  # type: ignore[misc]

    async def _ensure_board_lists(self) -> None:
        if self._list_id_to_name:
            return
        data = await self._request("GET", f"/boards/{self._board_id}/lists")
        self._list_id_to_name = {lst["id"]: lst["name"] for lst in data if lst.get("id")}

    async def poll_tickets(self) -> list[TicketData]:
        cards = await self._request(
            "GET",
            f"/boards/{self._board_id}/cards",
            params={
                "attachments": "true",
                "fields": "name,desc,shortLink,shortUrl,labels,idList,closed,members",
            },
        )
        if not isinstance(cards, list):
            logger.warning("Trello: unexpected poll response shape %r", type(cards))
            return []
        tickets: list[TicketData] = []
        for card in cards:
            if card.get("closed"):
                continue
            label_names = {(lbl.get("name") or "") for lbl in card.get("labels", [])}
            if not any(t in label_names for t in self._trigger_labels):
                continue
            if any(i in label_names for i in self._ignore_labels):
                continue
            tickets.append(self._parse_card(card))
        logger.info(
            "Polled %d Trello cards from board %s (after label filter)",
            len(tickets), self._board_id,
        )
        return tickets

    async def get_ticket(self, ticket_id: str) -> TicketData:
        card = await self._request(
            "GET", f"/cards/{ticket_id}", params={"attachments": "true"},
        )
        return self._parse_card(card)

    async def transition_ticket(self, ticket_id: str, status: str) -> None:
        if not self._status_to_list_id:
            raise ValueError(
                "Trello list mapping is empty; run the wizard or set "
                "tracker.trello.lists in project.yaml"
            )
        list_id = self._status_to_list_id.get(status)
        if list_id is None:
            raise ValueError(
                f"Unknown status {status!r}; mapped keys: "
                f"{sorted(self._status_to_list_id)}"
            )
        await self._request(
            "PUT", f"/cards/{ticket_id}", params={"idList": list_id},
        )
        logger.info("Trello: moved %s to list %s (%s)", ticket_id, status, list_id)

    async def add_comment(self, ticket_id: str, comment: str) -> None:
        await self._request(
            "POST", f"/cards/{ticket_id}/actions/comments",
            params={"text": comment},
        )
        logger.info("Trello: commented on %s", ticket_id)

    async def get_comments(self, ticket_id: str) -> list[TicketComment]:
        actions = await self._request(
            "GET", f"/cards/{ticket_id}/actions",
            params={"filter": "commentCard"},
        )
        if not isinstance(actions, list):
            return []
        results = [
            TicketComment(
                id=a.get("id", ""),
                author=(a.get("memberCreator") or {}).get("fullName", ""),
                created=(a.get("date") or "")[:10],
                body=(a.get("data") or {}).get("text", ""),
            )
            for a in actions
        ]
        results.reverse()
        return results

    async def get_status_history(self, ticket_id: str) -> list[StatusChange]:
        await self._ensure_board_lists()
        actions = await self._request(
            "GET", f"/cards/{ticket_id}/actions",
            params={"filter": "updateCard:idList"},
        )
        if not isinstance(actions, list):
            return []
        results: list[StatusChange] = []
        for a in actions:
            data = a.get("data") or {}
            before = (data.get("listBefore") or {}).get("id", "")
            after = (data.get("listAfter") or {}).get("id", "")
            results.append(StatusChange(
                created=(a.get("date") or "")[:10],
                from_status=self._list_id_to_name.get(before, before),
                to_status=self._list_id_to_name.get(after, after),
                author=(a.get("memberCreator") or {}).get("fullName", ""),
            ))
        results.reverse()
        return results

    async def list_transitions(self, ticket_id: str) -> list[str]:
        await self._ensure_board_lists()
        card = await self._request("GET", f"/cards/{ticket_id}")
        current = card.get("idList", "")
        return [name for lid, name in self._list_id_to_name.items() if lid != current]

    async def download_attachment(self, url: str) -> bytes:
        """Fetch attachment bytes. Trello-hosted URLs need OAuth1 headers."""
        host = urlparse(url).hostname or ""
        headers: dict[str, str] = {}
        if _is_trello_host(host):
            headers["Authorization"] = (
                f'OAuth oauth_consumer_key="{self._key}", '
                f'oauth_token="{self._token}"'
            )
        try:
            resp = await self._raw_client.get(url, headers=headers if headers else None)
            if resp.status_code == 200:
                return resp.content
            logger.warning(
                "Trello: attachment fetch returned %d for %s",
                resp.status_code, url,
            )
            return b""
        except Exception as e:
            logger.warning("Trello: attachment fetch failed for %s: %s", url, e)
            return b""

    def _parse_card(self, card: dict) -> TicketData:
        members = card.get("members") or []
        assignee = members[0].get("fullName") if members else None
        return TicketData(
            id=card.get("shortLink", card.get("id", "")),
            url=card.get("shortUrl", ""),
            summary=card.get("name", ""),
            description=card.get("desc", "") or "",
            acceptance_criteria="",
            labels=[lbl.get("name", "") for lbl in card.get("labels", []) if lbl.get("name")],
            priority="",
            sprint=None,
            linked_issues=[],
            assignee=assignee,
            reporter="",
            created=_card_created_at(card.get("id", "")),
            attachments=[
                {
                    "filename": a.get("name", ""),
                    "url": a.get("url", ""),
                    "mime_type": a.get("mimeType", ""),
                }
                for a in card.get("attachments", []) if a.get("url")
            ],
        )
