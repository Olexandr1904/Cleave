"""Jira Cloud adapter implementing TrackerInterface."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

from integrations.base.auth_failure import auth_failure_hook
from integrations.base.tracker import (
    StatusChange,
    TicketComment,
    TicketData,
    TrackerInterface,
)

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
        *, on_auth_failure: Callable[[int], None] | None = None,
        transition_via: list[str] | None = None,
        transition_dates: dict[str, list[str]] | None = None,
    ) -> None:
        # Every client this adapter builds reports a 401/403 through this hook.
        self._auth_hooks = {"response": [auth_failure_hook(on_auth_failure)]}
        self._url = url.rstrip("/")
        self._email = email
        self._token = token
        self._project_key = project_key
        self._trigger_labels = trigger_labels or ["ai-pipeline"]
        self._ignore_labels = ignore_labels or []
        self._statuses = statuses or {
            "todo": "To Do",
            "in_progress": "In Progress",
            "in_review": "In Review",
            "done": "Done",
        }
        # The statuses transition_ticket may step through on its way to a status it has no transition to, in order.
        self._transition_via = list(transition_via or [])
        # Per status (lower-cased): the date fields a move into it sets to today, for a workflow that requires them
        # on that transition. A field the ticket has a value in keeps it.
        self._transition_dates = {s.lower(): list(f) for s, f in (transition_dates or {}).items()}
        self._client = httpx.AsyncClient(
            base_url=f"{self._url}/rest/api/3",
            auth=(email, token),
            timeout=TIMEOUT,
            headers={"Accept": "application/json"},
            event_hooks=self._auth_hooks,
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
        issue_type = fields.get("issuetype")
        status = fields.get("status")

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
            attachments=[
                {
                    "filename": a.get("filename", ""),
                    "url": a.get("content", ""),
                    "mime_type": a.get("mimeType", ""),
                }
                for a in fields.get("attachment", [])
                if a.get("content")
            ],
            issue_type=issue_type.get("name", "") if issue_type else "",
            status=status.get("name", "") if status else "",
        )

    async def poll_tickets(self) -> list[TicketData]:
        """Fetch tickets matching trigger criteria."""
        jql = self._build_todo_jql()
        data = await self._request(
            "POST", "/search/jql",
            json={"jql": jql, "maxResults": 50, "fields": ["summary", "description", "status", "priority", "labels", "assignee", "reporter", "created", "issuetype", "customfield_10020", "issuelinks", "attachment"]},
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
        """Move a ticket to `status`: a transition whose name or target is `status`; else nothing when the ticket
        is in `status` already; else one step into a status of `transition_via` not entered yet, and again, at
        most `max_hops` steps. No other transition is ever taken: the first one a workflow offers can be
        "Resolve Issue". Raises ValueError, with the transitions offered last, when `status` is not reached."""
        target = status.lower()
        entered: set[str] = set()
        for hop in range(max_hops + 1):
            transitions = (await self._request("GET", f"/issue/{ticket_id}/transitions")).get("transitions", [])
            direct = next((t for t in transitions if target in (t.get("name", "").lower(), _to(t).lower())), None)
            if direct is not None:
                await self._post_transition(ticket_id, direct)
                logger.info("Transitioned %s to '%s'", ticket_id, status)
                return
            if hop == 0 and (await self._status(ticket_id)).lower() == target:
                return
            step = next((t for via in self._transition_via for t in transitions
                         if _to(t).lower() == via.lower() and via.lower() not in entered), None)
            if step is None or hop == max_hops:
                break
            await self._post_transition(ticket_id, step)
            entered.add(_to(step).lower())
            logger.info("Intermediate transition %s -> '%s' (hop %d toward '%s')",
                        ticket_id, _to(step), hop + 1, status)
        offered = [_to(t) or t.get("name", "") for t in transitions]
        raise ValueError(f"Cannot transition {ticket_id} to '{status}'. Available transitions: {offered}")

    async def _post_transition(self, ticket_id: str, transition: dict) -> None:
        """Take `transition`, with the date fields its status gets. Raises ValueError with Jira's own words when
        Jira refuses it ("Field Due date is required.")."""
        body: dict[str, Any] = {"transition": {"id": transition["id"]}}
        dates = await self._dates_to_set(ticket_id, _to(transition))
        if dates:
            body["fields"] = dates
        try:
            await self._request("POST", f"/issue/{ticket_id}/transitions", json=body)
        except httpx.HTTPStatusError as e:
            said = _jira_errors(e.response) if e.response.status_code in (400, 409, 422) else ""
            if not said:
                raise
            name = transition.get("name") or _to(transition)
            raise ValueError(f"Jira refused '{name}' on {ticket_id}: {said}") from e

    async def _dates_to_set(self, ticket_id: str, status: str) -> dict[str, str]:
        """The date fields a move into `status` sets to today: those of `transition_dates` the ticket has none in."""
        names = self._transition_dates.get(status.lower())
        if not names:
            return {}
        data = await self._request("GET", f"/issue/{ticket_id}", params={"fields": ",".join(names)})
        now = data.get("fields") or {}
        today = date.today().isoformat()
        return {n: today for n in names if not now.get(n)}

    async def _status(self, ticket_id: str) -> str:
        """The name of the ticket's status now."""
        data = await self._request("GET", f"/issue/{ticket_id}", params={"fields": "status"})
        return ((data.get("fields") or {}).get("status") or {}).get("name", "")

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

    async def get_comments(self, ticket_id: str) -> list[TicketComment]:
        """Fetch all comments on a ticket. ADF bodies are stripped to plain text."""
        data = await self._request(
            "GET", f"/issue/{ticket_id}?expand=changelog&fields=comment",
        )
        raw_comments = (
            data.get("fields", {}).get("comment", {}).get("comments", []) or []
        )
        out: list[TicketComment] = []
        for c in raw_comments:
            body = c.get("body", "")
            if isinstance(body, dict):
                body = _extract_adf_text(body)
            out.append(TicketComment(
                id=str(c.get("id", "")),
                author=(c.get("author") or {}).get("displayName", "?"),
                created=(c.get("created") or "")[:10],
                body=str(body),
            ))
        return out

    async def list_transitions(self, ticket_id: str) -> list[str]:
        """Return human-readable target names of currently available transitions.

        For each transition Jira returns both an action name (e.g. "Start
        Review") and a target state (`to.name`, e.g. "In Review"). Prefer the
        target name; fall back to the action name when the target is empty.
        """
        data = await self._request("GET", f"/issue/{ticket_id}/transitions")
        names: list[str] = []
        for t in data.get("transitions", []) or []:
            to_name = (t.get("to") or {}).get("name", "") or ""
            names.append(to_name or t.get("name", ""))
        return names

    async def download_attachment(self, url: str) -> bytes:
        """Fetch an attachment. Uses adapter-owned basic-auth credentials."""
        import base64
        creds = base64.b64encode(
            f"{self._email}:{self._token}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {creds}"}
        async with httpx.AsyncClient(timeout=TIMEOUT, event_hooks=self._auth_hooks) as client:
            resp = await client.get(url, headers=headers, follow_redirects=True)
            if resp.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"attachment fetch HTTP {resp.status_code}",
                    request=resp.request, response=resp,
                )
            return resp.content

    async def get_status_history(self, ticket_id: str) -> list[StatusChange]:
        """Walk Jira's changelog and return only status transitions."""
        data = await self._request(
            "GET", f"/issue/{ticket_id}?expand=changelog&fields=comment",
        )
        histories = data.get("changelog", {}).get("histories", []) or []
        out: list[StatusChange] = []
        for h in histories:
            created = (h.get("created") or "")[:10]
            author = (h.get("author") or {}).get("displayName", "?")
            for item in h.get("items", []) or []:
                if item.get("field") != "status":
                    continue
                out.append(StatusChange(
                    created=created,
                    from_status=item.get("fromString", "?") or "?",
                    to_status=item.get("toString", "?") or "?",
                    author=author,
                ))
        return out

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


def _to(transition: dict) -> str:
    """The name of the status a transition leads to ("" when Jira names none)."""
    return (transition.get("to") or {}).get("name", "") or ""


def _jira_errors(response: httpx.Response) -> str:
    """What Jira said in an error answer: its errorMessages, then its per-field errors ("" when none)."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    said = list(body.get("errorMessages") or []) + list((body.get("errors") or {}).values())
    return " ".join(str(s) for s in said)


def _extract_adf_text(adf: dict) -> str:
    """Extract plain text from Atlassian Document Format."""
    lines: list[str] = []

    def walk(node: dict, buf: list[str]) -> None:
        ntype = node.get("type")
        if ntype == "text":
            buf.append(node.get("text", ""))
            return
        if ntype == "hardBreak":
            buf.append("\n")
            return
        if ntype in {"media", "mediaInline"}:
            # Leaf node for a pasted/attached image. Keep a reference so the
            # agent knows the comment has a visual — the file itself lands in
            # meta/attachments/ via the issue's attachment list.
            attrs = node.get("attrs", {}) or {}
            name = attrs.get("alt") or attrs.get("id") or "attached"
            buf.append(f"[image: {name}]")
            return
        # Block-level nodes flush the current line buffer before/after.
        # mediaSingle/mediaGroup wrap media leaves — treat as block so the
        # placeholder gets flushed to its own line.
        block = ntype in {"paragraph", "listItem", "heading", "blockquote",
                          "codeBlock", "tableRow", "tableHeader", "tableCell",
                          "orderedList", "bulletList",
                          "mediaSingle", "mediaGroup"}
        if block:
            inner: list[str] = []
            for child in node.get("content", []) or []:
                walk(child, inner)
            text = "".join(inner).strip()
            if text:
                lines.append(text)
        else:
            for child in node.get("content", []) or []:
                walk(child, buf)

    for child in adf.get("content", []) or []:
        walk(child, [])
    return "\n".join(lines)
