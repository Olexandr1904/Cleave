# Ticket Attachments & Comments — Agent Usability Design

**Date:** 2026-05-14
**Status:** Approved

## Problem

Pipeline agents (ba, dev, qa) cannot reliably use ticket attachments
(crash logs, screenshots) or operator comments as input. Investigation of
MBMOB-14812 and MBMOB-14805 found the attachments were *fetched* to disk
but never *analyzed* — the agent gave up trying to reach them.

Four distinct defects:

1. **Reachability.** CLI agents run with `cwd = workspace/source/`.
   Attachments live in `meta/attachments/` and comments in
   `meta/comments.md` — both siblings of `source/`, outside the agent's
   tool reach. The agent's `Read`/`Glob`/`Grep`/`Bash` calls can't see
   them. In MBMOB-14812 the agent's fallback absolute-path `find` was
   also blocked by the permission gate.

2. **Useless truncation.** `agent_runtime.assemble_prompt` inlines
   `meta/` files but caps each at 5 KB (`_PER_FILE_CONTEXT_BYTES`). A
   785 KB logcat is truncated to its first 5 KB — JSON device metadata,
   not the stack trace. The agent receives noise, knows a logcat exists,
   tries to open the real file, and fails (see defect 1).

3. **Images dropped entirely.** Inlining calls `read_text("utf-8")`,
   which raises `UnicodeDecodeError` on PNG/JPG attachments; the file is
   then silently skipped. The model never sees images pasted into the
   ticket or its comments.

4. **Comment→image link lost.** Jira's ADF text extractor
   (`_extract_adf_text`) ignores `media` nodes. A comment that pastes a
   screenshot becomes plain text with no reference to the image file,
   so the agent has no signal that the comment has a visual.

Comments are otherwise already fetched into `comments.md`; their only
gaps are reachability (defect 1) and the 5 KB cap (defect 2).

## Approach

Four surgical changes. Three are provider-neutral; the ADF fix is
Jira-specific (Trello comments are already plain text).

### 1. Make `meta/` reachable — `claude_code_adapter.py`

`_run_cli` and `execute_in_workspace` gain an `add_dirs: list[str] | None`
parameter. When set, each directory is appended to the CLI command as
`--add-dir <dir>`. This grants the agent's built-in tools read access to
those directories while `cwd` stays at `source/`, so git operations are
unaffected.

### 2. Manifest instead of blind inlining — `agent_runtime.py`

`_execute_cli` passes `add_dirs=[str(workspace.meta_dir)]`.

`assemble_prompt` changes:

- Small text meta files (`ticket.md`, `comments.md`, `history.md`,
  `parent.md`) continue to be inlined, keeping the 5 KB safety cap.
- When any inlined file is truncated, append a pointer line:
  `(truncated — full file at <absolute path>)` so the agent knows to
  read the rest with its tools.
- The `attachments/` directory is no longer inlined as truncated bytes.
  Instead emit an **attachments manifest**: one line per file with its
  absolute path, byte size, and type (`text` / `image`), followed by an
  instruction telling the agent these files are available and should be
  read with its tools (crash logs, screenshots, etc.).
- As a convenience, text attachments under 5 KB may still be inlined in
  full alongside their manifest entry.

This applies uniformly to every CLI agent.

### 3. Preserve image references in comments — `jira_adapter.py`

`_extract_adf_text` handles `media`, `mediaSingle`, `mediaGroup`, and
`mediaInline` nodes. For each, emit a placeholder `[image: <name>]`,
where `<name>` is the node's `alt` attribute (Jira usually stores the
original filename there) or `[image attached]` if absent. The agent then
knows the comment has a visual and can find it in `meta/attachments/`.

### 4. Tests

- `test_agent_runtime.py` — manifest contains absolute attachment paths
  (not truncated bytes); an image attachment appears in the manifest;
  truncated text file gets the "full file at" pointer.
- `test_jira_adapter.py` — an ADF body with a `media` node yields an
  `[image: ...]` placeholder in extracted text.
- `claude_code_adapter` test — `--add-dir` appears in the CLI command
  when `add_dirs` is passed.

## Out of scope

- Correlating ADF media UUIDs to numeric attachment IDs for exact
  filename mapping — the `alt` attribute is sufficient in practice.
- Trello attachment/comment handling — Trello comments are already
  plain text; reachability fix (changes 1–2) already benefits Trello.
- Raising or removing the 5 KB inline cap — the manifest + reachability
  fix makes the cap a non-issue for large files.

## Data flow (after fix)

```
Jira issue
  ├─ fields.attachment ──► ticket_sync.refetch_ticket_data
  │                          └─► meta/attachments/<file>   (logcats, images)
  ├─ fields.comment ─────► _extract_adf_text (media → [image: ...])
  │                          └─► meta/comments.md
  └─ ...
                                       │
agent_runtime.assemble_prompt          │
  ├─ inline small text meta files  ◄───┘
  └─ attachments manifest (abs paths + types + instruction)
                                       │
agent_runtime._execute_cli             │
  └─ claude CLI: cwd=source/  --add-dir meta/   ◄── agent tools reach meta/
```
