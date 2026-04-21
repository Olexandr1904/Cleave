"""Jira Cloud adapter implementing TrackerInterface."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from integrations.base.tracker import TicketData, TrackerInterface

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]  # seconds
TIMEOUT = 30  # seconds


class JiraAdapter(TrackerInterface):
    """Jira Cloud REST API adapter."""

    def __init__(
        self,
        url: str,
        email: str,
        token: str,
        project_key: str,
        trigger_labels: list[str] | None = None,
        ignore_labels: list[str] | None = None,
        statuses: dict[str, str] | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._project_key = project_key
        self._trigger_labels = trigger_labels or ["ai-pipeline"]
        self._ignore_labels = ignore_labels or []
        self._statuses = statuses or {
            "todo": "To Do",
            "in_progress": "In Progress",
            "in_review": "In Review",
            "done": "Done",
        }
        self._client = httpx.AsyncClient(
            base_url=f"{self._url}/rest/api/3",
            auth=(email, token),
            timeout=TIMEOUT,
            headers={"Accept": "application/json"},
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        """Make an HTTP request with retries."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.request(method, path, **kwargs)
                if response.status_code in (401, 403):
                    raise httpx.HTTPStatusError(
                        f"Authentication failed: {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                if response.status_code == 204:
                    return {}
                return response.json()
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_error = e
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                    raise  # Don't retry auth failures
                if attempt < MAX_RETRIES - 1:
                    import asyncio
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    logger.warning(
                        "Jira request retry %d/%d for %s %s: %s",
                        attempt + 1, MAX_RETRIES, method, path, e,
                    )
        raise last_error  # type: ignore[misc]

    def _build_todo_jql(self) -> str:
        """Build JQL query — all tickets with trigger labels, any status."""
        label_clauses = " AND ".join(
            f'labels = "{l}"' for l in self._trigger_labels
        )
        jql = f'project = {self._project_key} AND {label_clauses}'
        ignore = ", ".join(f'"{l}"' for l in self._ignore_labels)
        if ignore:
            jql += f" AND labels NOT IN ({ignore})"
        jql += " ORDER BY priority ASC, created ASC"
        return jql

    @staticmethod
    def _parse_ticket(issue: dict) -> TicketData:
        """Parse a Jira issue into TicketData."""
        fields = issue.get("fields", {})

        # Extract description text from ADF or plain text
        desc = fields.get("description", "")
        if isinstance(desc, dict):
            # ADF format — extract text content
            desc = _extract_adf_text(desc)
        elif desc is None:
            desc = ""

        assignee = fields.get("assignee")
        reporter = fields.get("reporter")
        sprint_field = fields.get("sprint")
        priority = fields.get("priority")

        return TicketData(
            id=issue["key"],
            url=f"{issue.get('self', '').split('/rest/')[0]}/browse/{issue['key']}",
            summary=fields.get("summary", ""),
            description=str(desc),
            acceptance_criteria="",  # Extracted from description by BA agent
            labels=fields.get("labels", []),
            priority=priority.get("name", "") if priority else "",
            sprint=sprint_field.get("name", "") if isinstance(sprint_field, dict) else None,
            linked_issues=[
                {"key": link.get("outwardIssue", link.get("inwardIssue", {})).get("key", ""),
                 "type": link.get("type", {}).get("name", "")}
                for link in fields.get("issuelinks", [])
                if link.get("outwardIssue") or link.get("inwardIssue")
            ],
            assignee=assignee.get("displayName", "") if assignee else None,
            reporter=reporter.get("displayName", "") if reporter else "",
            created=fields.get("created", ""),
        )

    async def poll_tickets(self) -> list[TicketData]:
        """Fetch tickets matching trigger criteria."""
        jql = self._build_todo_jql()
        data = await self._request(
            "POST", "/search/jql",
            json={"jql": jql, "maxResults": 50, "fields": ["summary", "description", "status", "priority", "labels", "assignee", "reporter", "created", "issuetype", "customfield_10020", "issuelinks"]},
        )
        issues = data.get("issues", [])
        tickets = []
        for issue in issues:
            ticket = self._parse_ticket(issue)
            # Skip assigned tickets (only unassigned or bot-assigned)
            # This filtering is done at the orchestrator level for flexibility
            tickets.append(ticket)
        logger.info("Polled %d tickets from Jira (JQL: %s)", len(tickets), jql)
        return tickets

    async def get_ticket(self, ticket_id: str) -> TicketData:
        """Get full ticket details by ID."""
        data = await self._request("GET", f"/issue/{ticket_id}")
        return self._parse_ticket(data)

    async def transition_ticket(self, ticket_id: str, status: str, max_hops: int = 3) -> None:
        """Transition a ticket to a new status, stepping through intermediates if needed."""
        for hop in range(max_hops):
            trans_data = await self._request("GET", f"/issue/{ticket_id}/transitions")
            transitions = trans_data.get("transitions", [])

            # Look for direct match to target status
            target_transition = None
            for t in transitions:
                if t.get("name", "").lower() == status.lower():
                    target_transition = t
                    break
                if t.get("to", {}).get("name", "").lower() == status.lower():
                    target_transition = t
                    break

            if target_transition:
                await self._request(
                    "POST",
                    f"/issue/{ticket_id}/transitions",
                    json={"transition": {"id": target_transition["id"]}},
                )
                logger.info("Transitioned %s to '%s'", ticket_id, status)
                return

            # No direct path — try the first available forward transition
            if not transitions:
                break
            # Pick the first transition that isn't going backwards
            step = transitions[0]
            await self._request(
                "POST",
                f"/issue/{ticket_id}/transitions",
                json={"transition": {"id": step["id"]}},
            )
            step_name = step.get("to", {}).get("name", step.get("name", "?"))
            logger.info(
                "Intermediate transition %s -> '%s' (hop %d toward '%s')",
                ticket_id, step_name, hop + 1, status,
            )

        # Exhausted hops
        trans_data = await self._request("GET", f"/issue/{ticket_id}/transitions")
        available = [t.get("name", "") for t in trans_data.get("transitions", [])]
        raise ValueError(
            f"Cannot transition {ticket_id} to '{status}' after {max_hops} hops. "
            f"Available transitions: {available}"
        )

    async def add_comment(self, ticket_id: str, comment: str) -> None:
        """Post a comment to a ticket."""
        # Jira API v3 requires ADF format for comments
        await self._request(
            "POST",
            f"/issue/{ticket_id}/comment",
            json={
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": comment}
                            ],
                        }
                    ],
                }
            },
        )
        logger.info("Added comment to %s", ticket_id)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


def _extract_adf_text(adf: dict) -> str:
    """Extract plain text from Atlassian Document Format."""
    texts = []
    for content in adf.get("content", []):
        for item in content.get("content", []):
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
    return "\n".join(texts)
