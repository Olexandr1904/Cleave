"""Tests for orchestrator/ticket_prioritizer.py."""

from __future__ import annotations

import pytest

from config.schemas import (
    JiraConfig,
    LoadedProject,
    ProjectConfig,
    ProjectInfo,
    RepoConfig,
    RepoInfo,
)
from integrations.base.tracker import TicketData
from orchestrator.ticket_prioritizer import (
    PrioritizedTicket,
    filter_tickets,
    prioritize_and_route,
    prioritize_tickets,
    route_tickets,
    skip_blocked_tickets,
)


def _ticket(
    id: str = "TEST-1",
    labels: list[str] | None = None,
    priority: str = "Medium",
    sprint: str | None = None,
    assignee: str | None = None,
    linked_issues: list[dict] | None = None,
    created: str = "2026-01-01T00:00:00.000+0000",
) -> TicketData:
    return TicketData(
        id=id,
        url=f"https://jira.example.com/browse/{id}",
        summary=f"Summary for {id}",
        description="",
        labels=labels or [],
        priority=priority,
        sprint=sprint,
        assignee=assignee,
        linked_issues=linked_issues or [],
        created=created,
    )


def _project(**repo_overrides: dict) -> LoadedProject:
    repos = {
        "android-app": RepoConfig(
            repo=RepoInfo(id="android-app"),
            jira_repo_label="android",
        ),
        "ios-app": RepoConfig(
            repo=RepoInfo(id="ios-app"),
            jira_repo_label="ios",
        ),
    }
    return LoadedProject(
        config=ProjectConfig(
            project=ProjectInfo(id="test-project"),
            jira=JiraConfig(
                trigger_labels=["ai-ready"],
                ignore_labels=["do-not-automate", "manual"],
            ),
        ),
        repos=repos,
    )


class TestFilterTickets:
    def test_passes_valid_ticket(self):
        tickets = [_ticket(labels=["ai-ready", "android"])]
        result = filter_tickets(tickets, trigger_labels=["ai-ready"], ignore_labels=["do-not-automate"])
        assert len(result) == 1

    def test_rejects_missing_trigger_label(self):
        tickets = [_ticket(labels=["android"])]
        result = filter_tickets(tickets, trigger_labels=["ai-ready"], ignore_labels=[])
        assert len(result) == 0

    def test_rejects_ignore_label(self):
        tickets = [_ticket(labels=["ai-ready", "do-not-automate"])]
        result = filter_tickets(tickets, trigger_labels=["ai-ready"], ignore_labels=["do-not-automate"])
        assert len(result) == 0

    def test_passes_unassigned(self):
        tickets = [_ticket(labels=["ai-ready"], assignee=None)]
        result = filter_tickets(tickets, trigger_labels=["ai-ready"], ignore_labels=[])
        assert len(result) == 1

    def test_passes_bot_assigned(self):
        tickets = [_ticket(labels=["ai-ready"], assignee="Cleave Pipeline")]
        result = filter_tickets(tickets, trigger_labels=["ai-ready"], ignore_labels=[])
        assert len(result) == 1

    def test_accepts_human_assigned(self):
        """Labels are the signal — assignee doesn't affect filtering."""
        tickets = [_ticket(labels=["ai-ready"], assignee="John Doe")]
        result = filter_tickets(tickets, trigger_labels=["ai-ready"], ignore_labels=[])
        assert len(result) == 1

    def test_multiple_tickets_mixed(self):
        tickets = [
            _ticket(id="T-1", labels=["ai-ready"]),
            _ticket(id="T-2", labels=["other"]),
            _ticket(id="T-3", labels=["ai-ready", "manual"]),
            _ticket(id="T-4", labels=["ai-ready"], assignee="Human"),
        ]
        result = filter_tickets(tickets, trigger_labels=["ai-ready"], ignore_labels=["manual"])
        assert len(result) == 2
        assert [t.id for t in result] == ["T-1", "T-4"]


class TestRouteTickets:
    def test_routes_by_label(self):
        project = _project()
        tickets = [_ticket(labels=["ai-ready", "android"])]
        result = route_tickets(tickets, project)
        assert len(result) == 1
        assert result[0].repo_id == "android-app"
        assert result[0].project_id == "test-project"

    def test_routes_to_correct_repo(self):
        project = _project()
        tickets = [_ticket(labels=["ai-ready", "ios"])]
        result = route_tickets(tickets, project)
        assert len(result) == 1
        assert result[0].repo_id == "ios-app"

    def test_no_matching_label(self):
        project = _project()
        tickets = [_ticket(labels=["ai-ready", "unknown-label"])]
        result = route_tickets(tickets, project)
        assert len(result) == 0

    def test_first_match_wins(self):
        project = _project()
        tickets = [_ticket(labels=["ai-ready", "android", "ios"])]
        result = route_tickets(tickets, project)
        assert len(result) == 1
        # First matching label in ticket's labels list wins


class TestSkipBlockedTickets:
    def _entry(self, ticket: TicketData) -> PrioritizedTicket:
        return PrioritizedTicket(ticket=ticket, repo_id="r", project_id="p")

    def test_no_links_passes(self):
        entry = self._entry(_ticket())
        result = skip_blocked_tickets([entry], set())
        assert len(result) == 1

    def test_done_dependency_passes(self):
        ticket = _ticket(linked_issues=[
            {"key": "DEP-1", "type": "Blocks"},
        ])
        entry = self._entry(ticket)
        result = skip_blocked_tickets([entry], {"DEP-1"})
        assert len(result) == 1

    def test_undone_dependency_blocked(self):
        ticket = _ticket(linked_issues=[
            {"key": "DEP-1", "type": "Blocks"},
        ])
        entry = self._entry(ticket)
        result = skip_blocked_tickets([entry], set())
        assert len(result) == 0

    def test_non_blocking_link_ignored(self):
        ticket = _ticket(linked_issues=[
            {"key": "REL-1", "type": "Relates"},
        ])
        entry = self._entry(ticket)
        result = skip_blocked_tickets([entry], set())
        assert len(result) == 1

    def test_mixed_links(self):
        ticket = _ticket(linked_issues=[
            {"key": "REL-1", "type": "Relates"},
            {"key": "DEP-1", "type": "is blocked by"},
        ])
        entry = self._entry(ticket)
        # DEP-1 is not done → blocked
        result = skip_blocked_tickets([entry], set())
        assert len(result) == 0

        # DEP-1 is done → passes
        result = skip_blocked_tickets([entry], {"DEP-1"})
        assert len(result) == 1


class TestPrioritizeTickets:
    def _entry(self, ticket: TicketData) -> PrioritizedTicket:
        return PrioritizedTicket(ticket=ticket, repo_id="r", project_id="p")

    def test_sprint_first(self):
        entries = [
            self._entry(_ticket(id="NO-SPRINT", priority="Highest")),
            self._entry(_ticket(id="IN-SPRINT", priority="Low", sprint="Sprint 5")),
        ]
        result = prioritize_tickets(entries)
        assert result[0].ticket.id == "IN-SPRINT"

    def test_priority_ordering(self):
        entries = [
            self._entry(_ticket(id="LOW", priority="Low")),
            self._entry(_ticket(id="HIGH", priority="High")),
            self._entry(_ticket(id="MEDIUM", priority="Medium")),
        ]
        result = prioritize_tickets(entries)
        ids = [e.ticket.id for e in result]
        assert ids == ["HIGH", "MEDIUM", "LOW"]

    def test_age_tiebreaker(self):
        entries = [
            self._entry(_ticket(id="NEW", priority="Medium", created="2026-03-01T00:00:00")),
            self._entry(_ticket(id="OLD", priority="Medium", created="2026-01-01T00:00:00")),
        ]
        result = prioritize_tickets(entries)
        assert result[0].ticket.id == "OLD"

    def test_full_ordering(self):
        entries = [
            self._entry(_ticket(id="T1", priority="Low", created="2026-01-01T00:00:00")),
            self._entry(_ticket(id="T2", priority="High", sprint="Sprint 1", created="2026-02-01T00:00:00")),
            self._entry(_ticket(id="T3", priority="High", created="2026-01-15T00:00:00")),
            self._entry(_ticket(id="T4", priority="Medium", sprint="Sprint 1", created="2026-01-01T00:00:00")),
        ]
        result = prioritize_tickets(entries)
        ids = [e.ticket.id for e in result]
        # Sprint tickets first (T2 High, T4 Medium), then non-sprint (T3 High, T1 Low)
        assert ids == ["T2", "T4", "T3", "T1"]


class TestPrioritizeAndRoute:
    def test_full_pipeline(self):
        project = _project()
        tickets = [
            _ticket(id="T-1", labels=["ai-ready", "android"], priority="Low",
                    created="2026-03-01T00:00:00"),
            _ticket(id="T-2", labels=["ai-ready", "ios"], priority="High",
                    sprint="Sprint 5", created="2026-03-01T00:00:00"),
            _ticket(id="T-3", labels=["other"]),  # filtered out
            _ticket(id="T-4", labels=["ai-ready", "android"], priority="High",
                    created="2026-01-01T00:00:00"),
        ]
        result = prioritize_and_route(tickets, project)
        assert len(result) == 3
        # T-2 first (sprint + High), T-4 second (High, older), T-1 third (Low)
        assert result[0].ticket.id == "T-2"
        assert result[0].repo_id == "ios-app"
        assert result[1].ticket.id == "T-4"
        assert result[1].repo_id == "android-app"
        assert result[2].ticket.id == "T-1"

    def test_with_blocked_tickets(self):
        project = _project()
        tickets = [
            _ticket(id="T-1", labels=["ai-ready", "android"],
                    linked_issues=[{"key": "DEP-1", "type": "Blocks"}]),
            _ticket(id="T-2", labels=["ai-ready", "ios"]),
        ]
        result = prioritize_and_route(tickets, project, done_ticket_ids=set())
        # T-1 blocked (DEP-1 not done), only T-2 remains
        assert len(result) == 1
        assert result[0].ticket.id == "T-2"

    def test_empty_input(self):
        project = _project()
        result = prioritize_and_route([], project)
        assert result == []

    def test_no_done_ids_skips_dependency_check(self):
        project = _project()
        tickets = [
            _ticket(id="T-1", labels=["ai-ready", "android"],
                    linked_issues=[{"key": "DEP-1", "type": "Blocks"}]),
        ]
        # done_ticket_ids=None means skip dependency check entirely
        result = prioritize_and_route(tickets, project, done_ticket_ids=None)
        assert len(result) == 1


def test_filter_tickets_requires_all_trigger_labels():
    t1 = _ticket(id="A-1", labels=["ai-pipeline", "acme-mobile"])
    t2 = _ticket(id="A-2", labels=["ai-pipeline"])
    t3 = _ticket(id="A-3", labels=["acme-mobile"])

    result = filter_tickets(
        [t1, t2, t3],
        trigger_labels=["ai-pipeline", "acme-mobile"],
        ignore_labels=[],
    )

    assert [t.id for t in result] == ["A-1"]


def test_filter_tickets_empty_trigger_labels_rejects_all():
    t1 = _ticket(id="A-1", labels=["ai-pipeline"])
    result = filter_tickets([t1], trigger_labels=[], ignore_labels=[])
    assert result == []
