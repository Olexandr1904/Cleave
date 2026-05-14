# Ticket Attachments & Comments — Agent Usability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ticket attachments (crash logs, screenshots) and operator comments reliably usable by pipeline CLI agents.

**Architecture:** Three surgical changes. (1) Pass `meta/` to the Claude CLI via `--add-dir` so the agent's tools can reach attachments/comments while `cwd` stays at `source/`. (2) Replace blind 5 KB inlining of attachments with a manifest of absolute paths the agent reads itself. (3) Preserve image references when stripping Jira ADF comment bodies to text.

**Tech Stack:** Python 3.12, pytest, httpx, Claude Code CLI subprocess.

**Spec:** `docs/superpowers/specs/2026-05-14-ticket-attachments-comments-design.md`

---

### Task 1: Preserve image references in Jira ADF extraction

Jira's `_extract_adf_text` drops `media` nodes, so a comment that pastes a screenshot becomes text with no reference to the image. Emit an `[image: <name>]` placeholder instead.

**Files:**
- Modify: `integrations/jira/jira_adapter.py` — `_extract_adf_text`, function starts at line 278
- Test: `tests/integration/test_jira_adapter.py`

- [ ] **Step 1: Write the failing test**

Add to the end of `tests/integration/test_jira_adapter.py`:

```python
def test_extract_adf_text_preserves_media_reference() -> None:
    """A pasted image in an ADF body becomes an [image: name] placeholder
    so the agent knows the comment has a visual to inspect."""
    from integrations.jira.jira_adapter import _extract_adf_text

    adf = {
        "type": "doc", "version": 1,
        "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "See the crash below"},
            ]},
            {"type": "mediaSingle", "content": [
                {"type": "media", "attrs": {
                    "type": "file", "id": "abc-123",
                    "collection": "x", "alt": "crash-screenshot.png",
                }},
            ]},
        ],
    }
    text = _extract_adf_text(adf)
    assert "See the crash below" in text
    assert "[image: crash-screenshot.png]" in text


def test_extract_adf_text_media_falls_back_to_id() -> None:
    """When a media node has no alt filename, fall back to its id."""
    from integrations.jira.jira_adapter import _extract_adf_text

    adf = {
        "type": "doc", "version": 1,
        "content": [
            {"type": "mediaSingle", "content": [
                {"type": "media", "attrs": {"type": "file", "id": "uuid-9"}},
            ]},
        ],
    }
    assert "[image: uuid-9]" in _extract_adf_text(adf)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_jira_adapter.py::test_extract_adf_text_preserves_media_reference tests/integration/test_jira_adapter.py::test_extract_adf_text_media_falls_back_to_id -v`
Expected: FAIL — the placeholder strings are not in the extracted text (media nodes are currently ignored).

- [ ] **Step 3: Write minimal implementation**

In `integrations/jira/jira_adapter.py`, inside `_extract_adf_text`'s nested `walk` function, add a `media` leaf handler immediately after the `hardBreak` handler, and add `mediaSingle`/`mediaGroup` to the `block` set.

Current code:

```python
        if ntype == "hardBreak":
            buf.append("\n")
            return
        # Block-level nodes flush the current line buffer before/after
        block = ntype in {"paragraph", "listItem", "heading", "blockquote",
                          "codeBlock", "tableRow", "tableHeader", "tableCell",
                          "orderedList", "bulletList"}
```

Replace with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/integration/test_jira_adapter.py -v`
Expected: PASS — all jira adapter tests including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add integrations/jira/jira_adapter.py tests/integration/test_jira_adapter.py
git commit -m "fix(jira): preserve image references when flattening ADF comment bodies"
```

---

### Task 2: `--add-dir` support in the Claude Code CLI adapter

The CLI agent runs with `cwd=source/`; it cannot reach `meta/`. Add an `add_dirs` parameter that maps to the CLI's `--add-dir` flag.

**Files:**
- Modify: `integrations/llm/claude_code_adapter.py` — `execute_in_workspace` (line 153), `_run_cli` (line 272)
- Test: `tests/unit/test_claude_code_adapter.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_claude_code_adapter.py` (inside the file, after the `TestQuickQuery` class — top-level is fine):

```python
class TestAddDir:
    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")

    async def test_add_dirs_appended_to_cli_command(self, adapter):
        """add_dirs are passed through as --add-dir flags so the agent's
        tools can read directories outside its cwd (e.g. meta/)."""
        result_event = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode()
        mock_proc = _make_streaming_proc([result_event], [], rc=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await adapter.execute_in_workspace(
                prompt="test", cwd="/tmp/ws/source",
                allowed_tools=["read_file"],
                add_dirs=["/tmp/ws/meta"],
            )
        cmd_list = list(mock_exec.call_args[0])
        assert "--add-dir" in cmd_list
        idx = cmd_list.index("--add-dir")
        assert cmd_list[idx + 1] == "/tmp/ws/meta"

    async def test_no_add_dir_flag_when_add_dirs_omitted(self, adapter):
        """Without add_dirs the flag must not appear — keeps default runs clean."""
        result_event = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode()
        mock_proc = _make_streaming_proc([result_event], [], rc=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await adapter.execute_in_workspace(
                prompt="test", cwd="/tmp/ws/source",
                allowed_tools=["read_file"],
            )
        assert "--add-dir" not in list(mock_exec.call_args[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_claude_code_adapter.py::TestAddDir -v`
Expected: FAIL — `execute_in_workspace()` got an unexpected keyword argument `add_dirs`.

- [ ] **Step 3: Write minimal implementation**

In `integrations/llm/claude_code_adapter.py`:

(a) Add the `add_dirs` parameter to `execute_in_workspace`. Current signature ends:

```python
        pid_callback: Callable[[int], None] | None = None,
        progress_log_path: Path | None = None,
        raw_stream_path: Path | None = None,
    ) -> LLMResponse:
```

Change to:

```python
        pid_callback: Callable[[int], None] | None = None,
        progress_log_path: Path | None = None,
        raw_stream_path: Path | None = None,
        add_dirs: list[str] | None = None,
    ) -> LLMResponse:
```

(b) In the body of `execute_in_workspace`, the `return await self._run_cli(...)` call currently ends:

```python
            progress_log_path=progress_log_path,
            raw_stream_path=raw_stream_path,
        )
```

Change to:

```python
            progress_log_path=progress_log_path,
            raw_stream_path=raw_stream_path,
            add_dirs=add_dirs,
        )
```

(c) Add the `add_dirs` parameter to `_run_cli`. Current signature ends:

```python
        pid_callback: Callable[[int], None] | None = None,
        progress_log_path: Path | None = None,
        raw_stream_path: Path | None = None,
    ) -> LLMResponse:
```

Change to:

```python
        pid_callback: Callable[[int], None] | None = None,
        progress_log_path: Path | None = None,
        raw_stream_path: Path | None = None,
        add_dirs: list[str] | None = None,
    ) -> LLMResponse:
```

(d) In `_run_cli`, append the flags right after the allowed-tools block. Current code:

```python
        # Allowed tools
        if allowed_tools is not None:
            cc_tools = self._map_tools(allowed_tools)
            cmd.extend(["--allowedTools", ",".join(cc_tools) if cc_tools else ""])
```

Add immediately after it:

```python
        # Extra directories the agent's tools may read beyond cwd. Ticket
        # metadata (attachments, comments) lives in meta/, a sibling of the
        # source/ cwd — without this the agent cannot reach it.
        for extra_dir in add_dirs or []:
            cmd.extend(["--add-dir", extra_dir])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_claude_code_adapter.py -v`
Expected: PASS — all adapter tests including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add integrations/llm/claude_code_adapter.py tests/unit/test_claude_code_adapter.py
git commit -m "feat(adapter): --add-dir passthrough so CLI agents can read dirs outside cwd"
```

---

### Task 3: Pass `meta/` to the CLI agent as an add-dir

Wire `agent_runtime._execute_cli` to pass the workspace `meta_dir` through the new `add_dirs` parameter.

**Files:**
- Modify: `orchestrator/agent_runtime.py` — `_execute_cli`, the `execute_in_workspace` call at lines 474-487
- Test: `tests/unit/test_agent_runtime.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_agent_runtime.py` (top-level, after the `TestQuotaFailureClassification` class):

```python
class TestCliAddDirs:
    async def test_execute_cli_passes_meta_dir_as_add_dir(self, registry, workspace):
        """The CLI agent runs with cwd=source/, so meta/ must be passed as an
        add-dir or the agent cannot read attachments and comments."""
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        from integrations.llm.llm_interface import LLMResponse
        from orchestrator.agent_runtime import AgentRuntime

        captured: dict = {}

        class StubAdapter(ClaudeCodeAdapter):
            def __init__(self):
                pass

            async def execute_in_workspace(self, *args, **kwargs):
                captured.update(kwargs)
                return LLMResponse(
                    content="ok", input_tokens=10, output_tokens=5,
                    model="claude-sonnet-4-5",
                )

        runtime = AgentRuntime(registry, StubAdapter())
        await runtime.execute("dev-agent", workspace)

        assert captured.get("add_dirs") == [str(workspace.meta_dir)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_agent_runtime.py::TestCliAddDirs -v`
Expected: FAIL — `captured.get("add_dirs")` is `None`, not `[str(workspace.meta_dir)]`.

- [ ] **Step 3: Write minimal implementation**

In `orchestrator/agent_runtime.py`, `_execute_cli`, the `execute_in_workspace` call currently reads:

```python
            response = await asyncio.wait_for(
                adapter.execute_in_workspace(
                    prompt=prompt,
                    cwd=str(workspace.source_dir),
                    allowed_tools=allowed_tools if allowed_tools else None,
                    model=model,
                    max_turns=budget.max_cli_turns,
                    timeout=budget.wall_clock_seconds,
                    pid_callback=lambda pid: self.update_pid(ticket_id, pid),
                    progress_log_path=progress_log_path,
                    raw_stream_path=raw_stream_path,
                ),
                timeout=budget.wall_clock_seconds,
            )
```

Add the `add_dirs` argument right after `cwd=`:

```python
            response = await asyncio.wait_for(
                adapter.execute_in_workspace(
                    prompt=prompt,
                    cwd=str(workspace.source_dir),
                    add_dirs=[str(workspace.meta_dir)],
                    allowed_tools=allowed_tools if allowed_tools else None,
                    model=model,
                    max_turns=budget.max_cli_turns,
                    timeout=budget.wall_clock_seconds,
                    pid_callback=lambda pid: self.update_pid(ticket_id, pid),
                    progress_log_path=progress_log_path,
                    raw_stream_path=raw_stream_path,
                ),
                timeout=budget.wall_clock_seconds,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_agent_runtime.py::TestCliAddDirs -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/agent_runtime.py tests/unit/test_agent_runtime.py
git commit -m "feat(runtime): expose ticket meta/ dir to CLI agents via --add-dir"
```

---

### Task 4: Attachments manifest + truncation pointers in `assemble_prompt`

Stop inlining truncated attachment bytes (a 785 KB logcat truncated to 5 KB is useless). Emit a manifest of absolute paths instead; the agent reads the files itself (now reachable via Task 3). Also add a "full file at <path>" pointer whenever an inlined meta file is truncated.

**Files:**
- Modify: `orchestrator/agent_runtime.py` — `assemble_prompt` context block (lines 226-283), plus a new module-level helper
- Test: `tests/unit/test_agent_runtime.py` — update `test_includes_text_attachments` and `test_skips_binary_attachments`, add manifest tests

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_agent_runtime.py`, **replace** the existing `test_includes_text_attachments` and `test_skips_binary_attachments` methods inside `class TestAssemblePrompt` with these:

```python
    def test_attachments_manifest_lists_text_files_by_path(
        self, registry, mock_llm, workspace
    ):
        """Attachments are listed in a manifest by absolute path, not inlined
        as truncated bytes — a large crash log truncated to 5 KB is useless."""
        attachments_dir = workspace.meta_dir / "attachments"
        attachments_dir.mkdir()
        crash = attachments_dir / "crash.logcat"
        crash.write_text("FATAL: NullPointerException at Foo.kt:42\n" * 500)

        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert "## Ticket Attachments" in prompt
        assert str(crash) in prompt
        assert "text" in prompt

    def test_attachments_manifest_lists_images(
        self, registry, mock_llm, workspace
    ):
        """Image attachments appear in the manifest (the CLI Read tool can
        view them) instead of being silently dropped."""
        attachments_dir = workspace.meta_dir / "attachments"
        attachments_dir.mkdir()
        png = attachments_dir / "screenshot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\xfd")

        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert str(png) in prompt
        assert "image" in prompt

    def test_no_manifest_when_no_attachments(
        self, registry, mock_llm, workspace
    ):
        """No attachments dir -> no manifest section."""
        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert "## Ticket Attachments" not in prompt

    def test_truncated_meta_file_gets_full_path_pointer(
        self, registry, mock_llm, workspace
    ):
        """When an inlined meta file is truncated, the agent is told where the
        full file lives so it can read the rest with its tools."""
        from orchestrator.agent_runtime import _PER_FILE_CONTEXT_BYTES

        big = workspace.meta_dir / "comments.md"
        big.write_text("x" * (_PER_FILE_CONTEXT_BYTES + 1000))

        runtime = AgentRuntime(registry, mock_llm)
        agent = registry.get_agent("dev-agent")
        prompt = runtime.assemble_prompt(agent, workspace)

        assert f"full file at {big}" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_agent_runtime.py::TestAssemblePrompt -v`
Expected: FAIL — the four new tests fail (`## Ticket Attachments` absent, `full file at` absent, etc.).

- [ ] **Step 3: Add the manifest helper**

In `orchestrator/agent_runtime.py`, add this module-level function just above the `HARD_SAFETY_RULES` constant (after the `AgentRuntime` class):

```python
def _build_attachments_manifest(attachments_dir: Path) -> str:
    """Build a manifest of ticket attachments for the agent prompt.

    Lists each file's absolute path, size, and type (text/image). The agent
    reads them with its own tools — large crash logs truncate to noise if
    inlined, and images are not text. Returns "" when there are none.
    """
    if not attachments_dir.is_dir():
        return ""
    entries: list[str] = []
    for att in sorted(attachments_dir.iterdir()):
        if not att.is_file():
            continue
        try:
            att.read_text(encoding="utf-8")
            kind = "text"
        except (UnicodeDecodeError, OSError):
            kind = "image"
        size_kb = max(1, att.stat().st_size // 1024)
        entries.append(f"- {att} ({size_kb} KB, {kind})")
    if not entries:
        return ""
    return (
        "## Ticket Attachments\n\n"
        "These files are attached to the ticket and available on disk at the "
        "paths below. Read them with your tools — use Read for images and "
        "short text, Grep to search large logs. Crash logs and screenshots "
        "often contain the root cause; inspect them before analyzing code.\n\n"
        + "\n".join(entries)
    )
```

- [ ] **Step 4: Rewrite the context block in `assemble_prompt`**

In `orchestrator/agent_runtime.py`, replace the entire section from the `# 3. Workspace context files` comment through the `if context_sections:` block (current lines 226-283) with:

```python
        # 3. Workspace context files (read from meta_dir)
        # Note: pipeline reports live at source/ai_pipeline/<ticket>/ — agents
        # read them directly via tools, not via this context block.
        # Small text meta files (ticket.md, comments.md, ...) are inlined here.
        # Attachments are NOT inlined — see step 3b: a truncated crash log is
        # noise and images are not text. They go in a manifest instead.
        context_sections: list[str] = []
        context_dir = workspace.meta_dir
        total_bytes = 0

        def _include(path: Path, label: str) -> bool:
            """Read path as text and append a context section. Returns False if budget exhausted."""
            nonlocal total_bytes
            if total_bytes >= _TOTAL_CONTEXT_BYTES:
                return False
            try:
                file_content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                return True  # skip this file but keep going
            truncated = False
            if len(file_content) > _PER_FILE_CONTEXT_BYTES:
                file_content = file_content[:_PER_FILE_CONTEXT_BYTES]
                truncated = True
            remaining = _TOTAL_CONTEXT_BYTES - total_bytes
            if len(file_content) > remaining:
                file_content = file_content[:remaining]
                truncated = True
            if truncated:
                # Point the agent at the full file — it can read the rest with
                # its tools (meta_dir is passed via --add-dir).
                file_content += f"\n...(truncated — full file at {path})"
            context_sections.append(
                f"<context file=\"{label}\">\n{file_content}\n</context>"
            )
            total_bytes += len(file_content)
            return True

        if context_dir.exists():
            for ctx_file in sorted(context_dir.iterdir()):
                if not ctx_file.is_file():
                    continue
                if not _include(ctx_file, ctx_file.name):
                    logger.warning(
                        "Context budget %d B exhausted; skipping remaining files in %s",
                        _TOTAL_CONTEXT_BYTES, context_dir,
                    )
                    break

        if context_sections:
            prompt_body += "\n\n## Workspace Context\n\n" + "\n\n".join(context_sections)

        # 3b. Attachments manifest — absolute paths, not inlined bytes. The
        # agent reads these itself; meta_dir is reachable via --add-dir.
        manifest = _build_attachments_manifest(context_dir / "attachments")
        if manifest:
            prompt_body += "\n\n" + manifest
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_agent_runtime.py::TestAssemblePrompt -v`
Expected: PASS — all `TestAssemblePrompt` tests, including the four new ones. (`test_total_context_budget_caps_many_files` still passes: only non-attachment meta files are inlined now, and the test writes `ctx_*.txt` directly in `meta_dir`.)

- [ ] **Step 6: Commit**

```bash
git add orchestrator/agent_runtime.py tests/unit/test_agent_runtime.py
git commit -m "feat(runtime): attachments manifest + truncation pointers instead of blind inlining"
```

---

### Task 5: Full regression check

**Files:** none (verification only)

- [ ] **Step 1: Run the affected test modules**

Run: `python -m pytest tests/unit/test_agent_runtime.py tests/unit/test_claude_code_adapter.py tests/unit/test_claude_code_adapter_quota.py tests/integration/test_jira_adapter.py -v`
Expected: PASS — all tests green.

- [ ] **Step 2: Run the full unit + integration suite**

Run: `python -m pytest tests/unit tests/integration -q`
Expected: PASS — no regressions. If any pre-existing failures appear unrelated to attachments/comments/adapter, note them but do not fix in this plan.

- [ ] **Step 3: Lint the changed files**

Run: `ruff check integrations/jira/jira_adapter.py integrations/llm/claude_code_adapter.py orchestrator/agent_runtime.py`
Expected: no errors.

---

## Self-Review

**Spec coverage:**
- Spec change 1 (reachability / `--add-dir`) → Tasks 2 + 3.
- Spec change 2 (manifest instead of blind inlining, truncation pointer) → Task 4.
- Spec change 3 (ADF media references) → Task 1.
- Spec change 4 (tests) → tests embedded in Tasks 1–4, regression in Task 5.
- Spec "images reachable + listed, CLI Read handles them" → Task 4 manifest lists images by path; Task 3 makes them reachable. No image-specific decode code needed, matching the spec.

**Placeholder scan:** No TBD/TODO; every code step shows complete code.

**Type consistency:** `add_dirs: list[str] | None` is consistent across `execute_in_workspace`, `_run_cli`, and the `_execute_cli` call site. `_build_attachments_manifest(attachments_dir: Path) -> str` signature matches its single call site. `_include` keeps its `(path: Path, label: str) -> bool` signature.

**Note on existing tests:** Task 4 Step 1 explicitly *replaces* `test_includes_text_attachments` and `test_skips_binary_attachments` — their old assertions (`<context file="attachments/...">`, `screenshot.png not in prompt`) contradict the new manifest behavior by design.
