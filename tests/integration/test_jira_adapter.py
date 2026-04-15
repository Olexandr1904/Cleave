"""Integration tests for Jira adapter with mocked HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

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
        respx.get(
            "https://test.atlassian.net/rest/api/3/search",
        ).mock(return_value=httpx.Response(200, json={"issues": [MOCK_ISSUE]}))

        tickets = await adapter.poll_tickets()
        assert len(tickets) == 1
        assert tickets[0].id == "TEST-123"
        assert tickets[0].summary == "Add login button"
        assert "ai-ready" in tickets[0].labels

    @respx.mock
    async def test_poll_empty(self, adapter):
        respx.get(
            "https://test.atlassian.net/rest/api/3/search",
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
        trigger_labels=["ai-pipeline", "acme-mobile-android"],
    )
    jql = adapter._build_todo_jql()
    assert 'labels = "ai-pipeline"' in jql
    assert 'labels = "acme-mobile-android"' in jql
    assert jql.count("AND") >= 3  # project AND label1 AND label2 AND status
