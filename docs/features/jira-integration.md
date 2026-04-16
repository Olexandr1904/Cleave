# Feature: Jira Integration

**Status:** Planned
**Created:** 2026-04-07
**Updated:** 2026-04-15
**Author:** Oleksandr Brazhenko

## Description

Jira adapter behind the TrackerInterface. Polls Jira for tickets matching configured labels and statuses, transitions tickets through their lifecycle, and posts comments for pipeline progress updates.

## Requirements

- FR1: Poll tickets matching: has ALL `trigger_labels` (AND semantics), status = todo, not in `ignore_labels`, unassigned or bot-assigned
- FR2: Return ticket data: id, summary, description, labels, priority, sprint, linked issues, acceptance criteria
- FR3: Transition tickets between configured statuses (todo → in_progress → in_review → done)
- FR4: Post formatted comments to tickets for status updates
- FR5: Authentication via token + email from project config with env var resolution
- FR6: HTTP errors handled with retries (3 attempts with backoff)
- FR7: Ticket content sanitized before injection into agent prompts
- FR8: Implements abstract `TrackerInterface`

## Technical Approach

- `JiraAdapter` class implementing `TrackerInterface`
- Uses httpx async client for Jira REST API v3
- JQL queries built from config (project_key, trigger_labels, ignore_labels, statuses)
- Ticket data normalized into `TicketData` model and written to `context/ticket.json`
- Retry logic with exponential backoff for transient HTTP errors
- Input sanitization strips potential prompt injection patterns from ticket content

## Dependencies

- httpx for async HTTP
- Configuration Cascade for Jira project settings (url, token, email, labels, statuses)
- Abstract TrackerInterface from `integrations/base/`
- PM Agent and Project Setup Agent for orchestration and configuration

## Acceptance Criteria

- [ ] Polls tickets matching configured criteria via JQL
- [ ] Returns normalized ticket data with all required fields
- [ ] Transitions tickets between statuses
- [ ] Posts formatted comments
- [ ] Retries on HTTP errors with backoff
- [ ] Sanitizes ticket content before agent injection

## Change Log

| Date | Description |
|------|-------------|
| 2026-04-07 | Initial draft — seeded from PRD and architecture docs |
| 2026-04-15 | trigger_label renamed to trigger_labels (list, AND semantics) |
| 2026-04-16 | Fix integration tests: poll_tickets uses POST /search/jql (Jira Cloud API); update mocks from GET /search |
