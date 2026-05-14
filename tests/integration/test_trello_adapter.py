"""Integration test for TrelloAdapter against httpx.MockTransport."""

from __future__ import annotations

import httpx
import pytest

from integrations.trello.trello_adapter import TrelloAdapter


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _make_adapter(transport):
    a = TrelloAdapter(
        api_key="kkk", token="ttt", board_id="board-x",
        trigger_labels=["ai-pipeline"], ignore_labels=[],
        list_mapping={"todo": "L1", "in_progress": "L2", "in_review": "L3", "done": "L4"},
    )
    a._client = httpx.AsyncClient(
        base_url="https://api.trello.com/1",
        transport=transport,
        params={"key": "kkk", "token": "ttt"},
    )
    return a


@pytest.mark.asyncio
async def test_poll_tickets_end_to_end():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/1/boards/board-x/cards"
        assert request.url.params["key"] == "kkk"
        assert request.url.params["token"] == "ttt"
        return httpx.Response(200, json=[
            {
                "id": "abc12345600000000000aaaa", "shortLink": "Sl1",
                "shortUrl": "https://trello.com/c/Sl1",
                "name": "First", "desc": "body",
                "labels": [{"name": "ai-pipeline"}],
                "members": [], "attachments": [], "closed": False,
            },
        ])

    a = _make_adapter(_mock_transport(handler))
    tickets = await a.poll_tickets()
    assert len(tickets) == 1
    assert tickets[0].summary == "First"


@pytest.mark.asyncio
async def test_retry_after_honored_on_429():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.1"})
        return httpx.Response(200, json=[])

    a = _make_adapter(_mock_transport(handler))
    result = await a.poll_tickets()
    assert calls["n"] == 2
    assert result == []
