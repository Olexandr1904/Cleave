"""Tests for TrelloAdapter — each method against mocked httpx responses."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from integrations.base.tracker import StatusChange, TicketComment, TicketData
from integrations.trello.trello_adapter import TrelloAdapter


def _adapter(**overrides):
    kwargs = dict(
        api_key="kkk",
        token="ttt",
        board_id="board-abc",
        trigger_labels=["ai-pipeline"],
        ignore_labels=["wip"],
        list_mapping={
            "todo": "list-todo",
            "in_progress": "list-doing",
            "in_review": "list-review",
            "done": "list-done",
        },
    )
    kwargs.update(overrides)
    return TrelloAdapter(**kwargs)


def test_init_stores_credentials_and_mapping():
    a = _adapter()
    assert a._key == "kkk"
    assert a._token == "ttt"
    assert a._board_id == "board-abc"
    assert a._status_to_list_id == {
        "todo": "list-todo",
        "in_progress": "list-doing",
        "in_review": "list-review",
        "done": "list-done",
    }
    assert a._list_id_to_status == {
        "list-todo": "todo",
        "list-doing": "in_progress",
        "list-review": "in_review",
        "list-done": "done",
    }


@pytest.mark.asyncio
async def test_poll_tickets_filters_by_trigger_label():
    a = _adapter()
    a._request = AsyncMock(return_value=[
        {
            "id": "abc12345600000000000aaaa", "shortLink": "Sl1",
            "shortUrl": "https://trello.com/c/Sl1",
            "name": "First", "desc": "body",
            "labels": [{"name": "ai-pipeline"}],
            "members": [], "attachments": [], "closed": False,
        },
        {
            "id": "abc12345600000000000bbbb", "shortLink": "Sl2",
            "shortUrl": "https://trello.com/c/Sl2",
            "name": "Second", "desc": "",
            "labels": [{"name": "other"}],
            "members": [], "attachments": [], "closed": False,
        },
    ])
    tickets = await a.poll_tickets()
    assert len(tickets) == 1
    assert tickets[0].id == "Sl1"
    a._request.assert_awaited_once()
    method, path = a._request.await_args.args[:2]
    assert method == "GET"
    assert path == "/boards/board-abc/cards"


@pytest.mark.asyncio
async def test_poll_tickets_skips_ignore_label():
    a = _adapter()
    a._request = AsyncMock(return_value=[
        {
            "id": "abc12345600000000000aaaa", "shortLink": "Sl1", "shortUrl": "",
            "name": "First", "desc": "",
            "labels": [{"name": "ai-pipeline"}, {"name": "wip"}],
            "members": [], "attachments": [], "closed": False,
        },
    ])
    assert await a.poll_tickets() == []


@pytest.mark.asyncio
async def test_poll_tickets_skips_closed():
    a = _adapter()
    a._request = AsyncMock(return_value=[
        {
            "id": "abc12345600000000000aaaa", "shortLink": "Sl1", "shortUrl": "",
            "name": "First", "desc": "",
            "labels": [{"name": "ai-pipeline"}],
            "members": [], "attachments": [], "closed": True,
        },
    ])
    assert await a.poll_tickets() == []


@pytest.mark.asyncio
async def test_get_ticket_returns_ticket_data():
    a = _adapter()
    a._request = AsyncMock(return_value={
        "id": "abc12345600000000000aaaa", "shortLink": "Sl1",
        "shortUrl": "https://trello.com/c/Sl1",
        "name": "First", "desc": "body",
        "labels": [{"name": "ai-pipeline"}],
        "members": [{"fullName": "Alice"}],
        "attachments": [
            {"name": "spec.pdf", "url": "https://download.trello.com/x.pdf", "mimeType": "application/pdf"},
        ],
        "closed": False,
    })
    t = await a.get_ticket("Sl1")
    assert isinstance(t, TicketData)
    assert t.id == "Sl1"
    assert t.summary == "First"
    assert t.description == "body"
    assert t.assignee == "Alice"
    assert t.attachments == [
        {"filename": "spec.pdf", "url": "https://download.trello.com/x.pdf", "mime_type": "application/pdf"},
    ]


@pytest.mark.asyncio
async def test_transition_ticket_moves_card_to_mapped_list():
    a = _adapter()
    a._request = AsyncMock(return_value={})
    await a.transition_ticket("Sl1", "in_progress")
    method, path = a._request.await_args.args[:2]
    assert method == "PUT"
    assert path == "/cards/Sl1"
    assert a._request.await_args.kwargs["params"]["idList"] == "list-doing"


@pytest.mark.asyncio
async def test_transition_ticket_unknown_status_raises():
    a = _adapter()
    with pytest.raises(ValueError, match="Unknown status"):
        await a.transition_ticket("Sl1", "rejected")


@pytest.mark.asyncio
async def test_transition_ticket_empty_mapping_raises():
    a = _adapter(list_mapping={})
    with pytest.raises(ValueError, match="list mapping is empty"):
        await a.transition_ticket("Sl1", "in_progress")


@pytest.mark.asyncio
async def test_add_comment_posts_to_actions():
    a = _adapter()
    a._request = AsyncMock(return_value={})
    await a.add_comment("Sl1", "hello world")
    method, path = a._request.await_args.args[:2]
    assert method == "POST"
    assert path == "/cards/Sl1/actions/comments"
    assert a._request.await_args.kwargs["params"]["text"] == "hello world"


@pytest.mark.asyncio
async def test_get_comments_reverses_order():
    """Trello returns newest-first; adapter returns oldest-first."""
    a = _adapter()
    a._request = AsyncMock(return_value=[
        {"id": "c2", "date": "2026-05-10T12:00:00Z",
         "memberCreator": {"fullName": "Bob"},
         "data": {"text": "newer"}},
        {"id": "c1", "date": "2026-05-09T10:00:00Z",
         "memberCreator": {"fullName": "Alice"},
         "data": {"text": "older"}},
    ])
    comments = await a.get_comments("Sl1")
    assert [c.id for c in comments] == ["c1", "c2"]
    assert comments[0].author == "Alice"
    assert comments[0].body == "older"
    assert isinstance(comments[0], TicketComment)


@pytest.mark.asyncio
async def test_get_status_history_maps_actions():
    a = _adapter()
    a._request = AsyncMock(side_effect=[
        [
            {"id": "list-todo", "name": "To Do"},
            {"id": "list-doing", "name": "Doing"},
            {"id": "list-done", "name": "Done"},
            {"id": "list-review", "name": "Review"},
        ],
        [
            {"id": "a1", "date": "2026-05-09T10:00:00Z",
             "memberCreator": {"fullName": "Alice"},
             "data": {"listBefore": {"id": "list-todo"}, "listAfter": {"id": "list-doing"}}},
        ],
    ])
    history = await a.get_status_history("Sl1")
    assert len(history) == 1
    assert isinstance(history[0], StatusChange)
    assert history[0].from_status == "To Do"
    assert history[0].to_status == "Doing"
    assert history[0].author == "Alice"


@pytest.mark.asyncio
async def test_list_transitions_returns_other_list_names():
    a = _adapter()
    a._request = AsyncMock(side_effect=[
        [
            {"id": "list-todo", "name": "To Do"},
            {"id": "list-doing", "name": "Doing"},
            {"id": "list-done", "name": "Done"},
        ],
        {"idList": "list-todo"},
    ])
    names = await a.list_transitions("Sl1")
    assert set(names) == {"Doing", "Done"}


@pytest.mark.asyncio
async def test_download_attachment_uses_oauth_header_for_trello_host():
    a = _adapter()
    a._raw_client = AsyncMock()
    a._raw_client.get = AsyncMock(return_value=type("R", (), {"status_code": 200, "content": b"PDF"})())
    data = await a.download_attachment("https://download.trello.com/x.pdf")
    assert data == b"PDF"
    call_kwargs = a._raw_client.get.await_args.kwargs
    assert "Authorization" in call_kwargs["headers"]
    assert 'oauth_consumer_key="kkk"' in call_kwargs["headers"]["Authorization"]
    assert 'oauth_token="ttt"' in call_kwargs["headers"]["Authorization"]


@pytest.mark.asyncio
async def test_download_attachment_no_auth_for_external_host():
    a = _adapter()
    a._raw_client = AsyncMock()
    a._raw_client.get = AsyncMock(return_value=type("R", (), {"status_code": 200, "content": b"DATA"})())
    await a.download_attachment("https://dropbox.com/x.pdf")
    call_kwargs = a._raw_client.get.await_args.kwargs
    assert "headers" not in call_kwargs or "Authorization" not in (call_kwargs.get("headers") or {})


def test_card_created_at_decoded_from_id():
    from integrations.trello.trello_adapter import _card_created_at
    # Trello card ID: first 8 hex chars = unix timestamp
    # 0x60000000 == 1610612736 == 2021-01-14T08:25:36Z
    result = _card_created_at("60000000abcdefabcdefabcd")
    assert result.startswith("2021-01-14")


@pytest.mark.asyncio
async def test_request_raises_on_sustained_429():
    """After MAX_RETRIES of 429, _request raises a real HTTPStatusError, not TypeError."""
    import httpx
    from integrations.trello.trello_adapter import TrelloAdapter

    a = _adapter()
    # Replace the underlying client so every call returns 429
    request_obj = httpx.Request("GET", "https://api.trello.com/1/boards/board-abc/cards")

    class _AlwaysRateLimited:
        async def request(self, method, path, **kwargs):
            return httpx.Response(429, headers={"Retry-After": "0.01"}, request=request_obj)

    a._client = _AlwaysRateLimited()
    with pytest.raises(httpx.HTTPStatusError, match="Rate limited"):
        await a.poll_tickets()


def test_is_trello_host_rejects_spoofed_names():
    """Host-suffix check must NOT accept nottrello.com or atlassian.com.evil.tld."""
    from integrations.trello.trello_adapter import _is_trello_host
    assert _is_trello_host("trello.com") is True
    assert _is_trello_host("download.trello.com") is True
    assert _is_trello_host("api.trello.com") is True
    assert _is_trello_host("atlassian.com") is True
    assert _is_trello_host("download.atlassian.com") is True
    assert _is_trello_host("nottrello.com") is False
    assert _is_trello_host("trello.com.evil.tld") is False
    assert _is_trello_host("evil-trello.com") is False
    assert _is_trello_host("") is False


@pytest.mark.asyncio
async def test_request_handles_malformed_retry_after():
    """Empty or non-numeric Retry-After falls back to 1.0s, doesn't crash."""
    import httpx
    from integrations.trello.trello_adapter import TrelloAdapter

    a = _adapter()
    request_obj = httpx.Request("GET", "https://api.trello.com/1/boards/board-abc/cards")
    calls = {"n": 0}

    class _OneBadRetryAfterThenOK:
        async def request(self, method, path, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "bogus"}, request=request_obj)
            return httpx.Response(200, content=b"[]", request=request_obj)

    a._client = _OneBadRetryAfterThenOK()
    result = await a.poll_tickets()
    assert calls["n"] == 2
    assert result == []
