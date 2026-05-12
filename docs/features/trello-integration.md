# Feature: Trello Integration

**Status:** In Progress
**Created:** 2026-05-12
**Updated:** 2026-05-12
**Author:** Oleksandr Brazhenko

## Description

Trello adapter behind the TrackerInterface. Polls Trello board cards matching configured trigger labels, transitions cards between lists, posts comments, and downloads attachments with OAuth1 headers.

## Requirements

- FR1: Poll cards with trigger label (AND semantics), skip closed cards and ignore labels
- FR2: Return ticket data: id (shortLink), summary, description, labels, assignee, attachments, created (decoded from card ID)
- FR3: Transition cards by moving to the configured list for the target status
- FR4: Post comments to cards
- FR5: Auth via API key + token query params; attachment downloads use OAuth1-style Authorization header for trello.com/atlassian.com hosts
- FR6: HTTP errors handled with retries (3 attempts with backoff), Retry-After honored on 429
- FR7: Fuzzy list-name autodetect (`list_autodetect.py`) maps board lists to Cleave status keys — pure function, no I/O
- FR8: Implements abstract `TrackerInterface`

## Technical Approach

- `TrelloAdapter` class in `integrations/trello/trello_adapter.py` implementing `TrackerInterface`
- Uses httpx async client for Trello REST API v1
- Card ID first 8 hex digits decoded as Unix timestamp for `created` field
- Board list name cache (`_list_id_to_name`) lazily populated on first `get_status_history` / `list_transitions` call
- `list_autodetect.py` provides pure-function fuzzy matching for wizard and Atlas fallback
- Separate `_raw_client` for attachment downloads (no base_url or default auth params)

## Change Log

- 2026-05-12: Task 6 — TrelloAdapter implementing all 9 TrackerInterface methods; list_autodetect helper; unit + integration tests
- 2026-05-12: Fix rate-limit exhaustion TypeError; tighten OAuth host check to reject suffix-spoofed names; parse Retry-After header safely
