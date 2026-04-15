"""Ticket prioritizer — filters, routes, and orders tickets for processing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from config.schemas import LoadedProject, RepoConfig
from integrations.base.tracker import TicketData

logger = logging.getLogger(__name__)

# Jira priority ordering (lower index = higher priority)
PRIORITY_ORDER = ["Highest", "High", "Medium", "Low", "Lowest"]


@dataclass
class PrioritizedTicket:
    """A ticket matched to its target repo, ready for workspace creation."""
    ticket: TicketData
    repo_id: str
    project_id: str


def filter_tickets(
    tickets: list[TicketData],
    trigger_labels: list[str],
    ignore_labels: list[str],
    bot_name: str = "Sickle Pipeline",
) -> list[TicketData]:
    """Filter tickets by label and assignee rules.

    AC3: Must have all trigger labels, must not have ignore labels,
    must be unassigned or bot-assigned.
    """
    if not trigger_labels:
        return []

    result = []
    for ticket in tickets:
        # Must have ALL trigger labels
        missing = [l for l in trigger_labels if l not in ticket.labels]
        if missing:
            logger.debug(
                "Skipping %s: missing trigger labels %s", ticket.id, missing
            )
            continue

        # Must not have any ignore labels
        if any(label in ticket.labels for label in ignore_labels):
            logger.debug("Skipping %s: has ignore label", ticket.id)
            continue

        # Must be unassigned or bot-assigned
        if ticket.assignee is not None and ticket.assignee != bot_name:
            logger.debug("Skipping %s: assigned to '%s'", ticket.id, ticket.assignee)
            continue

        result.append(ticket)

    logger.info("Filtered %d → %d tickets", len(tickets), len(result))
    return result


def route_tickets(
    tickets: list[TicketData],
    project: LoadedProject,
) -> list[PrioritizedTicket]:
    """Route each ticket to its target repo via jira_repo_label matching.

    AC4: Each repo has a jira_repo_label. A ticket is routed to the repo
    whose label appears in the ticket's labels list.
    """
    # Build label → (repo_id, project_id) map
    label_map: dict[str, tuple[str, str]] = {}
    project_id = project.config.project.id
    for repo_id, repo_config in project.repos.items():
        if repo_config.jira_repo_label:
            label_map[repo_config.jira_repo_label] = (repo_id, project_id)

    result = []
    for ticket in tickets:
        matched_repo = None
        for label in ticket.labels:
            if label in label_map:
                repo_id, proj_id = label_map[label]
                matched_repo = PrioritizedTicket(
                    ticket=ticket,
                    repo_id=repo_id,
                    project_id=proj_id,
                )
                break

        if matched_repo:
            result.append(matched_repo)
        else:
            logger.warning(
                "Ticket %s has no matching repo label (labels: %s)",
                ticket.id, ticket.labels,
            )

    return result


def skip_blocked_tickets(
    tickets: list[PrioritizedTicket],
    done_ticket_ids: set[str],
) -> list[PrioritizedTicket]:
    """Skip tickets whose linked dependencies are not Done.

    AC6: A ticket is blocked if it has a linked issue (type "Blocks" or
    "is blocked by") that is NOT in the done set.
    """
    blocking_link_types = {"Blocks", "is blocked by"}

    result = []
    for entry in tickets:
        ticket = entry.ticket
        blocked = False

        for link in ticket.linked_issues:
            link_type = link.get("type", "")
            link_key = link.get("key", "")
            if link_type in blocking_link_types and link_key not in done_ticket_ids:
                logger.info(
                    "Skipping %s: blocked by %s (status not Done)",
                    ticket.id, link_key,
                )
                blocked = True
                break

        if not blocked:
            result.append(entry)

    return result


def _priority_sort_key(entry: PrioritizedTicket) -> tuple[int, int, str]:
    """Sort key: sprint membership → priority → age (created timestamp).

    AC5: Prioritize by sprint > priority > age.
    Returns tuple where lower values = higher priority.
    """
    ticket = entry.ticket

    # Sprint membership: tickets in a sprint come first (0), others after (1)
    sprint_key = 0 if ticket.sprint else 1

    # Priority: map to index (lower = higher priority)
    try:
        priority_key = PRIORITY_ORDER.index(ticket.priority)
    except ValueError:
        priority_key = len(PRIORITY_ORDER)  # Unknown priority goes last

    # Age: older tickets first (earlier ISO timestamp sorts lower)
    age_key = ticket.created or "9999"

    return (sprint_key, priority_key, age_key)


def prioritize_tickets(
    tickets: list[PrioritizedTicket],
) -> list[PrioritizedTicket]:
    """Sort tickets by sprint membership → priority → age.

    AC5: Sprint tickets first, then by priority field, then by age (oldest first).
    """
    return sorted(tickets, key=_priority_sort_key)


def prioritize_and_route(
    tickets: list[TicketData],
    project: LoadedProject,
    done_ticket_ids: set[str] | None = None,
    bot_name: str = "Sickle Pipeline",
) -> list[PrioritizedTicket]:
    """Full PM Agent pipeline: filter → route → skip blocked → prioritize.

    Args:
        tickets: Raw tickets from poll cycle.
        project: Loaded project with repo configs.
        done_ticket_ids: Set of ticket IDs in Done status (for dependency check).
        bot_name: Name used by the bot for assignee check.

    Returns:
        Ordered list of (ticket, repo_id, project_id) ready for workspace creation.
    """
    jira_config = project.config.jira

    # AC3: Filter by labels and assignee
    filtered = filter_tickets(
        tickets,
        trigger_labels=jira_config.trigger_labels,
        ignore_labels=jira_config.ignore_labels,
        bot_name=bot_name,
    )

    # AC4: Route to repos
    routed = route_tickets(filtered, project)

    # AC6: Skip blocked tickets
    if done_ticket_ids is not None:
        routed = skip_blocked_tickets(routed, done_ticket_ids)

    # AC5: Prioritize
    ordered = prioritize_tickets(routed)

    logger.info(
        "Prioritized %d tickets: %s",
        len(ordered),
        [(t.ticket.id, t.repo_id) for t in ordered],
    )
    return ordered
