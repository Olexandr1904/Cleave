"""Integration tests for Jira adapter with mocked HTTP."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from integrations.base.tracker import StatusChange, TicketComment
from integrations.jira.jira_adapter import JiraAdapter


@pytest.fixture
def adapter():
    return JiraAdapter(
        url="https://test.atlassian.net",
        email="test@example.com",
        token="fake-token",
        project_key="TEST",
        trigger_labels=["ai-ready"],
        ignore_labels=["blocked"],
    )


MOCK_ISSUE = {
    "key": "TEST-123",
    "self": "https://test.atlassian.net/rest/api/3/issue/10001",
    "fields": {
        "summary": "Add login button",
        "description": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Implement login"}]}
        ]},
        "labels": ["ai-ready", "repo:test-repo"],
        "priority": {"name": "High"},
        "sprint": None,
        "issuelinks": [],
        "assignee": None,
        "reporter": {"displayName": "Alice"},
    },
}


class TestJiraPollTickets:
    @respx.mock
    async def test_poll_returns_tickets(self, adapter):
        respx.post(
            "https://test.atlassian.net/rest/api/3/search/jql",
        ).mock(return_value=httpx.Response(200, json={"issues": [MOCK_ISSUE]}))

        tickets = await adapter.poll_tickets()
        assert len(tickets) == 1
        assert tickets[0].id == "TEST-123"
        assert tickets[0].summary == "Add login button"
        assert "ai-ready" in tickets[0].labels

    @respx.mock
    async def test_poll_empty(self, adapter):
        respx.post(
            "https://test.atlassian.net/rest/api/3/search/jql",
        ).mock(return_value=httpx.Response(200, json={"issues": []}))

        tickets = await adapter.poll_tickets()
        assert tickets == []


class TestJiraGetTicket:
    @respx.mock
    async def test_get_ticket(self, adapter):
        respx.get(
            "https://test.atlassian.net/rest/api/3/issue/TEST-123",
        ).mock(return_value=httpx.Response(200, json=MOCK_ISSUE))

        ticket = await adapter.get_ticket("TEST-123")
        assert ticket.id == "TEST-123"
        assert ticket.description == "Implement login"


class TestJiraTransition:
    @respx.mock
    async def test_transition_ticket(self, adapter):
        respx.get(
            "https://test.atlassian.net/rest/api/3/issue/TEST-123/transitions",
        ).mock(return_value=httpx.Response(200, json={
            "transitions": [
                {"id": "31", "name": "In Progress", "to": {"name": "In Progress"}},
                {"id": "41", "name": "Done", "to": {"name": "Done"}},
            ]
        }))
        respx.post(
            "https://test.atlassian.net/rest/api/3/issue/TEST-123/transitions",
        ).mock(return_value=httpx.Response(204))

        await adapter.transition_ticket("TEST-123", "In Progress")

    @respx.mock
    async def test_transition_not_found(self, adapter):
        respx.get(
            "https://test.atlassian.net/rest/api/3/issue/TEST-123/transitions",
        ).mock(return_value=httpx.Response(200, json={"transitions": []}))

        with pytest.raises(ValueError, match="Cannot transition"):
            await adapter.transition_ticket("TEST-123", "Nonexistent")


class TestJiraComment:
    @respx.mock
    async def test_add_comment(self, adapter):
        respx.post(
            "https://test.atlassian.net/rest/api/3/issue/TEST-123/comment",
        ).mock(return_value=httpx.Response(201, json={"id": "1"}))

        await adapter.add_comment("TEST-123", "Pipeline started")


class TestJiraRetry:
    @respx.mock
    async def test_retries_on_timeout(self, adapter):
        route = respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-123")
        route.side_effect = [
            httpx.TimeoutException("timeout"),
            httpx.Response(200, json=MOCK_ISSUE),
        ]

        ticket = await adapter.get_ticket("TEST-123")
        assert ticket.id == "TEST-123"

    @respx.mock
    async def test_auth_failure_no_retry(self, adapter):
        respx.get(
            "https://test.atlassian.net/rest/api/3/issue/TEST-123",
        ).mock(return_value=httpx.Response(401))

        with pytest.raises(httpx.HTTPStatusError):
            await adapter.get_ticket("TEST-123")


def test_build_jql_ands_multiple_trigger_labels():
    adapter = JiraAdapter(
        url="https://example.atlassian.net",
        email="bot@example.com",
        token="tok",
        project_key="ACME",
        trigger_labels=["ai-pipeline", "acme-mobile"],
    )
    jql = adapter._build_todo_jql()
    assert 'labels = "ai-pipeline"' in jql
    assert 'labels = "acme-mobile"' in jql
    assert jql.count("AND") >= 3  # project AND label1 AND label2 AND status


@pytest.mark.asyncio
async def test_get_comments_returns_ticketcomment_list() -> None:
    adapter = JiraAdapter(
        url="https://x", email="e", token="t", project_key="P",
    )
    raw = {
        "fields": {
            "comment": {
                "comments": [
                    {
                        "id": "1001",
                        "author": {"displayName": "Alice"},
                        "created": "2026-05-10T12:00:00.000+0000",
                        "body": "first comment",
                    },
                    {
                        "id": "1002",
                        "author": {"displayName": "Bob"},
                        "created": "2026-05-11T09:00:00.000+0000",
                        "body": {
                            "type": "doc", "version": 1,
                            "content": [{"type": "paragraph", "content": [
                                {"type": "text", "text": "ADF comment"},
                            ]}],
                        },
                    },
                ],
            },
        },
    }
    with patch.object(adapter, "_request", AsyncMock(return_value=raw)):
        comments = await adapter.get_comments("PROJ-1")

    assert len(comments) == 2
    assert isinstance(comments[0], TicketComment)
    assert comments[0].id == "1001"
    assert comments[0].author == "Alice"
    assert comments[0].body == "first comment"
    assert comments[0].created == "2026-05-10"
    assert comments[1].body == "ADF comment"


@pytest.mark.asyncio
async def test_get_status_history_returns_status_changes() -> None:
    adapter = JiraAdapter(url="https://x", email="e", token="t", project_key="P")
    raw = {
        "changelog": {
            "histories": [
                {
                    "created": "2026-05-10T08:00:00.000+0000",
                    "author": {"displayName": "Alice"},
                    "items": [
                        {"field": "status", "fromString": "To Do",
                         "toString": "In Progress"},
                        {"field": "labels", "fromString": "", "toString": "x"},
                    ],
                },
                {
                    "created": "2026-05-11T15:00:00.000+0000",
                    "author": {"displayName": "Bob"},
                    "items": [
                        {"field": "status", "fromString": "In Progress",
                         "toString": "In Review"},
                    ],
                },
            ],
        },
    }
    with patch.object(adapter, "_request", AsyncMock(return_value=raw)):
        history = await adapter.get_status_history("PROJ-1")

    assert len(history) == 2  # non-status items skipped
    assert all(isinstance(h, StatusChange) for h in history)
    assert history[0].from_status == "To Do"
    assert history[0].to_status == "In Progress"
    assert history[0].created == "2026-05-10"
    assert history[1].to_status == "In Review"


@pytest.mark.asyncio
async def test_download_attachment_returns_bytes(monkeypatch) -> None:
    adapter = JiraAdapter(url="https://x", email="e", token="t", project_key="P")
    response = MagicMock()
    response.status_code = 200
    response.content = b"file bytes"
    response.request = MagicMock()

    class DummyClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url, headers=None, follow_redirects=False):
            return response

    monkeypatch.setattr("integrations.jira.jira_adapter.httpx.AsyncClient", DummyClient)
    data = await adapter.download_attachment("https://x/file")
    assert data == b"file bytes"


@pytest.mark.asyncio
async def test_list_transitions_returns_to_names() -> None:
    adapter = JiraAdapter(url="https://x", email="e", token="t", project_key="P")
    raw = {"transitions": [
        {"id": "21", "name": "Start Review", "to": {"name": "In Review"}},
        {"id": "31", "name": "Send Back",    "to": {"name": "To Do"}},
        {"id": "41", "name": "Close",        "to": {"name": ""}},  # falls back to name
    ]}
    with patch.object(adapter, "_request", AsyncMock(return_value=raw)):
        names = await adapter.list_transitions("PROJ-1")
    assert names == ["In Review", "To Do", "Close"]
