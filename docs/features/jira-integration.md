# Feature: Jira Integration

**Status:** Implemented
**Created:** 2026-04-07
**Updated:** 2026-05-12
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

## Provider-aware in_review_status (2026-05-12)

`_on_ticket_done` in the orchestrator now reads `jira.statuses.in_review` only when `tracker.provider == "jira"`. For other providers (e.g. Trello) the value defaults to `""`, which `on_ticket_done` treats as a no-op transition. Previously the jira field was read unconditionally (harmless but incorrect abstraction). The inline rationale comment points at the adapter (not the config object) as the source of Trello transitions.

## TrackerConfig wrapper (2026-05-12)

`ProjectConfig.jira` and `RepoConfig.jira` replaced by `ProjectConfig.tracker` and `RepoConfig.tracker` (type `TrackerConfig`). `TrackerConfig` holds `provider` ("jira" | "trello"), `jira: JiraConfig`, and `trello: TrelloConfig`, mirroring the `VCSConfig` pattern. A back-compat loader shim transparently lifts legacy top-level `jira:` blocks into `tracker.jira` so existing config files continue to load without modification. All call-sites updated to `project.config.tracker.jira.*` and `repo_config.tracker.jira.*`.

## Tracker abstraction expansion (2026-05-11)

`TrackerInterface` extended with `get_comments`, `get_status_history`, `download_attachment`, and `list_transitions` to remove Jira-specific HTTP plumbing from the orchestrator (formerly accessed via `_tracker._request`, `_email`, `_token`). New `TicketComment` and `StatusChange` dataclasses standardize the return shapes. Future Trello/GitLab adapters implement the same surface.

- `get_comments` implemented on `JiraAdapter`: walks `fields.comment.comments`, stripping ADF.
- `get_status_history` implemented on `JiraAdapter`: walks `changelog.histories[].items[]`, filters `field=status`.
- `download_attachment` implemented on `JiraAdapter`: adapter-owned basic auth, no leakage of `_email`/`_token`.
- `list_transitions` implemented on `JiraAdapter`: prefers `to.name`, falls back to `name`.
- `RepoConfig.jira_repo_label` renamed to `tracker_label` (loader accepts old key as alias).

## Per-project tracker build with provider dispatch (2026-05-12)

`main.py` now builds one tracker per configured project at startup using the module-level `_build_tracker_for_project(cfg, project_id)` helper, which dispatches on `cfg.provider`. The old single-first-project Jira block is replaced with a loop over all projects; `Orchestrator(trackers=...)` receives the completed dict directly. `on_project_added` calls the same helper so hot-reload behavior is consistent with startup. A stub Trello branch is present but lazy-imports `TrelloAdapter` — it will only fire once Task 6 lands. `CommandHandler` gains a `get_trackers` resolver kwarg and `set_trackers_resolver()` method; the legacy `set_tracker(tracker)` becomes a deprecated shim wrapping the tracker in a lambda. Phase 1 (Checkpoint A) complete.
