# Dashboard: Ticket Card Redesign

**Status:** Design approved · 2026-04-27

## Goal

Replace the cramped, ID-only ticket cards on the board with larger, title-first cards that surface what an operator actually scans for: what the ticket is about, what state it's in, and what action they can take next.

## Motivation

Today's cards show only the ticket ID, repo, state, and a tight footer of icons. Operators must click through to the detail view to see the title — every time. Errors crowd the title slot on FAILED cards. Iter chip and pause/delete buttons compete for the same cramped footer. The board reads as a wall of opaque IDs.

## Decisions

| # | Question | Choice |
|---|----------|--------|
| 1 | Layout direction | **A (compact tile)**, expanded — title in body, four explicit lines |
| 2 | Title source | **B** — persist `title` in `state.json` (new field on `WorkspaceState`) |
| 3 | Backfill strategy | Best-effort: when the dashboard scans a workspace and `title` is empty, parse `meta/ticket.md` first line and write it back to `state.json` |
| 4 | Setup workspace title | Static "Workspace setup" |
| 5 | Error placement | New line 3 (red), pushes meta and footer down |
| 6 | Iter rendering | Plain muted text in the meta line ("· iter 3"), no chip |
| 7 | Pause/delete location | Top-right of line 1 as icon buttons (pause is contextual; delete always present) |

## Card Anatomy

Width: **300px** (was 230px). Padding: 12px 14px. Font/colors match existing dashboard tokens.

```
┌──────────────────────────────────────────────────┐ ← inset 3px state stripe
│ TICKET-ID              [⏸] [✕]                   │  line 1 (always)
│ Title text up to two lines, ellipsis if   [STATE]│  line 2 (always)
│   longer than that.                              │
│ Error message (red)                              │  line 3 (conditional)
│ repo · 3d ago · iter 3                           │  line 4 (always)
│ PR #1486                              [Approve]  │  line 5 (conditional)
└──────────────────────────────────────────────────┘
```

### Line 1 — header

- **Left:** ticket ID (bold, `#e6edf3`).
- **Right:** pause/resume icon, then delete icon. Both are 22×22 transparent buttons with a 1px border (`#30363d`). Delete is always rendered. Pause is conditional (rules below).

### Line 2 — title + state badge

- **Left (flex):** title — weight 600, 14px, `#e6edf3`, `line-height: 1.35`. Wraps to **at most 2 lines**, then `text-overflow: ellipsis`. `title` attribute carries the full string for hover.
- **Right:** existing `state-badge` element, top-aligned, `flex-shrink: 0`.

When `title` is missing for a fresh workspace before backfill runs, render the title slot empty (state badge keeps its position via the right-aligned flex child).

### Line 3 — status note (conditional)

Mutually exclusive, in priority order:

1. `error` set → red error text (`#f85149`, 11px), wraps with `word-break: break-word`. The full error stays in a `title` tooltip in case it's clipped on a small screen.
2. `current_state == MANUAL_CONTROL` → "You have control" in `#d2a8ff` (replaces today's `card-manual-label`).
3. Neither → line omitted entirely (no spacer, card collapses).

### Line 4 — meta

Always rendered. Single line, 11px, `#8b949e`. Format:

```
{repo_id} · {timeAgo(started_at)} · iter {totalIters}
```

Separators are `·` in `#30363d`. The iter clause is omitted when total iterations is 0. The repo clause is omitted when `repo_id` is empty (e.g., the setup workspace technically still has one — we keep showing it).

### Line 5 — footer (conditional)

A flex row with PR link on the left, contextual button on the right. **Omitted** if the card has neither a PR link nor a contextual button.

- **PR link:** `PR #{n}` in `#58a6ff`, only when `pr_url` is set.
- **Contextual button (right side):**
  - `AWAITING_APPROVAL` → primary "Approve" button (blue fill, white text)
  - dimmed terminal states with a workspace on disk → "Clean" button
  - otherwise nothing

The pause/cancel/delete icons that used to live in this footer have moved to line 1.

## State-Specific Behavior

| State | Stripe color | Pause icon | Other notes |
|-------|--------------|------------|-------------|
| ANALYSIS / DEV / SCOPE_CHECK / QA / PUSHED / PR_REVIEW | none | `⏸` (Pause) | normal active card |
| PAUSED | blue (`#79c0ff`) | `▶` (Resume) | opacity 0.85 (today's value) |
| AWAITING_APPROVAL | yellow (`#e3b341`) | hidden | line 5 shows Approve button |
| MANUAL_CONTROL | purple (`#d2a8ff`) | hidden | line 3 shows "You have control" |
| BLOCKED / FAILED | red (`#f85149`) | hidden | line 3 shows error |
| DEFERRED / NEW | none | hidden | normal card, may have empty footer |
| DONE / ARCHIVED / SETUP_DONE | none | hidden | dimmed (`opacity: 0.45`, `0.7` on hover); Clean button on line 5 if `workspace_root` exists |

Stripe assignments preserve today's CSS classes (`card-blocked`, `card-awaiting`, `card-paused`, `card-manual`) — the redesign does not introduce stripes for states that don't have one today.

The pauseable list (where `⏸` shows) is unchanged from today: `ANALYSIS, DEV, SCOPE_CHECK, QA, PUSHED, PR_REVIEW`. PAUSED swaps to `▶`. All other states render no pause icon at all (the slot collapses).

## Data Model Changes

### `WorkspaceState` ([workspace/workspace.py](workspace/workspace.py))

Add a single new field:

```python
@dataclass
class WorkspaceState:
    ...
    title: str | None = None
```

Existing `_load_state` already filters unknown fields via `dataclasses.fields`, so old `state.json` files without `title` continue to load (with `title = None`).

### Title population

Three paths populate `title`:

1. **New tickets** — `WorkspaceManager.create_ticket_workspace` (or its caller) sets `title` from the polled `TicketData.summary` at workspace creation time. The TicketData is already fetched today; the orchestrator just needs to pass it through.
2. **Setup workspace** — `WorkspaceManager.create_setup_workspace` (or equivalent) sets `title = "Workspace setup"` unconditionally.
3. **Backfill for existing workspaces** — see next section.

### Backfill on dashboard scan

In [dashboard/web.py](dashboard/web.py) `_scan_all_workspaces`, when a state file loads with `title in (None, "")`:

- If `meta/ticket.md` exists: read first line, strip leading `# `, drop the `TICKET-ID:` prefix if present, take the rest as the title. Write the updated state back via the workspace's atomic save.
- If the workspace dir is named `setup`: write `title = "Workspace setup"`.
- Otherwise: leave it empty (no fallback string — line 2 just renders without text).

The scan path becomes mildly write-heavy on the first dashboard hit after deploy. Each backfill writes to one state file once, then never again. This is acceptable; the alternative (separate migration script) adds operator burden for what is fundamentally an idempotent fix.

### API surface

`_scan_all_workspaces` already returns a dict per workspace. Add `"title": data.get("title", "")` to that dict. The board endpoint pipes it through unchanged.

## Frontend Changes

### [dashboard/static/js/board.js](dashboard/static/js/board.js)

Rewrite `renderCard(ws)`. New structure (pseudocode):

```javascript
function renderCard(ws) {
  const stateVal = ws.current_state || 'NEW';
  const dimmed = ['DONE', 'ARCHIVED', 'SETUP_DONE'].includes(stateVal);
  const cardClass = ['card', stateClassFor(stateVal), dimmed && 'card-dimmed']
                      .filter(Boolean).join(' ');

  const pauseIcon = renderPauseIcon(ws, stateVal);   // ⏸ / ▶ / ''
  const deleteIcon = renderDeleteIcon(ws);

  const note = renderLine3(ws, stateVal);            // error / manual / ''
  const meta = `${esc(ws.repo_id || '')} · ${esc(timeAgo(ws.started_at))}` +
               (totalIters > 0 ? ` · iter ${totalIters}` : '');
  const footer = renderFooter(ws, stateVal);         // PR link + contextual btn or ''

  return `
    <div class="${cardClass}" data-ticket="${esc(ws.ticket_id)}">
      <div class="card-line1">
        <span class="card-id">${esc(ws.ticket_id)}</span>
        <span class="card-line1-spacer"></span>
        ${pauseIcon}
        ${deleteIcon}
      </div>
      <div class="card-line2">
        <div class="card-title" title="${esc(ws.title || '')}">${esc(ws.title || '')}</div>
        ${stateBadgeHtml(stateVal)}
      </div>
      ${note}
      <div class="card-meta">${meta}</div>
      ${footer}
    </div>`;
}
```

Helpers (`renderPauseIcon`, `renderLine3`, `renderFooter`) live in the same file, kept small. Existing event-binding code (`querySelectorAll('[data-action="pause"]')` etc.) keeps working as long as the `data-action` attributes are preserved on the new icon buttons.

### [dashboard/static/style.css](dashboard/static/style.css)

Replace today's `.card`, `.card-header`, `.card-ticket`, `.card-repo`, `.card-footer`, `.card-time`, `.card-iter`, `.card-error`, `.card-manual-label` rules with a fresh block scoped to the new line classes (`.card-line1`, `.card-line2`, `.card-title`, `.card-meta`, `.card-error`, `.card-manual`, `.card-footer`). The state-stripe modifiers (`.card-blocked`, `.card-awaiting`, `.card-paused`, `.card-manual` on the card root, etc.), state badges, `.card-dimmed`, and `.cards-grid` rule are unchanged.

Width changes from 230px to 300px. The `.cards-grid` `flex-wrap` already handles the reflow — fewer cards per row, no other change needed.

The icon button class `.card-icon-btn` is new. Hover styling matches the existing close-button hover (`opacity` transition, `border-color: #58a6ff`).

## What Stays the Same

- Health strip rendering above the board.
- Project grouping + sort order in `renderBoard`.
- Approve/Pause/Unpause/Delete/Clean fetch endpoints.
- Detail view (`detail.js`) — out of scope; this redesign touches board cards only.
- Empty state ("No projects yet"), wizard modal, sidebar.

## Out of Scope

- Detail view styling.
- Sort/filter UI changes (still has the existing "Hide done" toggle).
- Long-form ticket descriptions on cards.
- Live title updates if a Jira ticket is renamed after the workspace is created (backfill is one-shot — stale titles stay stale unless the workspace is recreated; this is consistent with how `branch` and other persisted fields behave today).

## Risks / Edge Cases

- **`meta/ticket.md` parse fails** (file present but no `# ID: title` first line) → title stays empty, no error logged at warning level, dashboard doesn't crash. Worst case: card renders with empty title slot until next reload.
- **Title contains HTML-ish characters** → `esc()` wraps it on the way in. Already standard in board.js.
- **Title is very long (200+ chars)** → 2-line clamp + ellipsis handles it. Tooltip shows the full string.
- **State badge wider than expected** (e.g., `MANUAL_CONTROL` rendered as text) → keep the current short labels (`MANUAL`, `AWAITING`) where they exist; if a label still wraps to two lines, the card grows by one line — acceptable.

## Verification

1. Existing workspace with no `title` in state → after first dashboard hit, `state.json` has the parsed title; page refresh shows the title.
2. New workspace created via the orchestrator → `state.json` has `title` from `TicketData.summary` immediately.
3. Setup workspace → "Workspace setup" appears, no `meta/ticket.md` needed.
4. FAILED card with an error → error text on line 3, no Pause icon, no PR/footer.
5. AWAITING_APPROVAL card → Approve button on line 5, yellow stripe, no Pause icon.
6. PAUSED card → resume `▶` icon, blue stripe, opacity ~0.9.
7. MANUAL_CONTROL card → purple "You have control" line 3, no Pause icon.
8. DONE card with workspace on disk → dimmed, Clean button on line 5.
9. Card with neither PR link nor contextual button → line 5 omitted, card is shorter.
