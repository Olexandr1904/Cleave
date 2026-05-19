from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.agent_failure_summarizer import extract_log_slice


def _make_ws(tmp_path: Path, stage_log_lines: list[str] | None = None,
             pipeline_log: str = "") -> SimpleNamespace:
    """Build a workspace-shaped namespace with logs_dir, reports_dir, state."""
    logs = tmp_path / "logs"
    logs.mkdir()
    reports = tmp_path / "source" / "ai_pipeline" / "T-1"
    reports.mkdir(parents=True)
    if stage_log_lines is not None:
        (logs / "qa-agent.events.log").write_text("\n".join(stage_log_lines) + "\n")
    if pipeline_log:
        (reports / "pipeline-log.md").write_text(pipeline_log)
    state = SimpleNamespace(
        ticket_id="T-1",
        title="Crash on submit",
        current_state="BLOCKED",
        previous_state="QA",
        stage_iterations={"qa": 2},
    )
    return SimpleNamespace(logs_dir=logs, reports_dir=reports, state=state)


def test_slice_contains_all_section_headers(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, stage_log_lines=["[t] event=heartbeat"],
                  pipeline_log="- step 1\n")
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    for header in ("=== TICKET ===", "=== STAGE HISTORY ===",
                   "=== OUTPUT FILES ===", "=== CRASH SIGNALS ===",
                   "=== LOG TAIL ==="):
        assert header in slice_, f"missing {header} in slice"


def test_slice_ticket_section_includes_metadata(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, stage_log_lines=["[t] x"])
    slice_ = extract_log_slice(ws, "qa", raw_error="exit code 137")
    assert "T-1" in slice_
    assert "Crash on submit" in slice_
    assert "qa" in slice_
    assert "qa: 2" in slice_ or "iteration" in slice_.lower()
    assert "exit code 137" in slice_


def test_crash_signals_extracts_errors_and_system_events(tmp_path: Path) -> None:
    log = [
        "[10:00] heartbeat elapsed=30s",
        "[10:01] event=tool_result is_error=False len=10",
        "[10:02] event=tool_result is_error=True len=200 preview='OutOfMemoryError'",
        "[10:03] event=system subtype=task_started",
        "[10:04] heartbeat elapsed=60s",
        "[10:05] Caused by: java.io.EOFException",
    ]
    ws = _make_ws(tmp_path, stage_log_lines=log)
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    crash = slice_.split("=== CRASH SIGNALS ===")[1].split("=== LOG TAIL ===")[0]
    assert "is_error=True" in crash
    assert "OutOfMemoryError" in crash
    assert "task_started" in crash
    assert "EOFException" in crash
    # Heartbeats are noise — must not be in crash signals.
    assert "heartbeat elapsed" not in crash


def test_output_files_section_lists_presence(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, stage_log_lines=["[t] x"])
    (ws.reports_dir / "ba-agent-output.md").write_text("ok")
    (ws.reports_dir / "dev-agent-output.md").write_text("ok")
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    files = slice_.split("=== OUTPUT FILES ===")[1].split("=== CRASH SIGNALS ===")[0]
    assert "ba-agent-output.md: present" in files
    assert "dev-agent-output.md: present" in files
    assert "qa-agent-output.md: missing" in files


def test_slice_respects_6kb_cap(tmp_path: Path) -> None:
    log = [f"[t] line {i} " + "x" * 200 for i in range(200)]
    ws = _make_ws(tmp_path, stage_log_lines=log)
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    assert len(slice_.encode("utf-8")) <= 6 * 1024


def test_crash_signals_preserved_when_truncating(tmp_path: Path) -> None:
    log = (
        [f"[t] event=tool_result is_error=True line {i}" for i in range(5)]
        + [f"[t] heartbeat padding line {i} " + "x" * 200 for i in range(200)]
    )
    ws = _make_ws(tmp_path, stage_log_lines=log)
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    crash = slice_.split("=== CRASH SIGNALS ===")[1].split("=== LOG TAIL ===")[0]
    # All 5 error lines must survive even when LOG TAIL is head-truncated.
    for i in range(5):
        assert f"is_error=True line {i}" in crash


def test_missing_events_log_returns_skeleton(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path, stage_log_lines=None)
    slice_ = extract_log_slice(ws, "qa", raw_error=None)
    # No log file — sections still present, CRASH SIGNALS/LOG TAIL are empty.
    assert "=== TICKET ===" in slice_
    tail = slice_.split("=== LOG TAIL ===")[1].strip()
    assert tail in ("", "(no log file)") or len(tail) < 50


def test_returns_with_warning_when_base_sections_exceed_cap(tmp_path: Path, caplog) -> None:
    """When TICKET/STAGE HISTORY/OUTPUT FILES/CRASH SIGNALS exceed 6 KB on
    their own, the function returns the slice anyway and logs a warning."""
    import logging
    # Stuff CRASH SIGNALS with many large is_error lines.
    log = [
        f"[t] event=tool_result is_error=True " + "x" * 800 + f" line {i}"
        for i in range(20)
    ]
    big_pipeline_log = "x" * 4000
    ws = _make_ws(tmp_path, stage_log_lines=log, pipeline_log=big_pipeline_log)
    huge_raw_error = "y" * 400  # _ticket_section truncates raw_error to 400
    with caplog.at_level(logging.WARNING):
        slice_ = extract_log_slice(ws, "qa", raw_error=huge_raw_error)
    # All five section headers still present.
    for header in ("=== TICKET ===", "=== STAGE HISTORY ===",
                   "=== OUTPUT FILES ===", "=== CRASH SIGNALS ===",
                   "=== LOG TAIL ==="):
        assert header in slice_
    # Warning was logged about base sections exceeding cap.
    assert any(
        "exceed cap" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )


def test_truncated_label_when_tail_empties(tmp_path: Path) -> None:
    """When the log file exists but the truncation loop empties LOG TAIL,
    the label is '(truncated)', not '(no log file)'."""
    # Many huge log lines force tail to empty during the truncation loop.
    log = [f"[t] tail line {i} " + "x" * 200 for i in range(200)]
    # Crash signals also large so the tail-truncation loop must run to empty.
    log = (
        [f"[t] event=tool_result is_error=True " + "x" * 500 + f" line {i}"
         for i in range(20)]
        + log
    )
    ws = _make_ws(tmp_path, stage_log_lines=log)
    slice_ = extract_log_slice(ws, "qa", raw_error="z" * 400)
    tail = slice_.split("=== LOG TAIL ===")[1].strip()
    # Should be the new truncated label, not the no-log-file one.
    assert tail == "(truncated)" or "(truncated)" in tail


# ---------------------------------------------------------------------------
# Tests for configure_summarizer and summarize_agent_failure (Task 4)
# ---------------------------------------------------------------------------

from orchestrator.agent_failure_summarizer import (  # noqa: E402
    configure_summarizer,
    summarize_agent_failure,
)


class _FakeMessageBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeMessageBlock(text)]


class _FakeClient:
    """Minimal stand-in for anthropic.AsyncAnthropic."""

    def __init__(self, response_text: str | Exception = "") -> None:
        self._response_text = response_text
        self.calls: list[dict] = []

        class _Messages:
            def __init__(self, parent: "_FakeClient") -> None:
                self._parent = parent

            async def create(self, **kwargs):
                self._parent.calls.append(kwargs)
                if isinstance(self._parent._response_text, Exception):
                    raise self._parent._response_text
                return _FakeResponse(self._parent._response_text)

        self.messages = _Messages(self)


def _ws(tmp_path: Path) -> SimpleNamespace:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "qa-agent.events.log").write_text(
        "[10:00] event=tool_result is_error=True preview='OOM'\n"
    )
    reports = tmp_path / "source" / "ai_pipeline" / "T-1"
    reports.mkdir(parents=True)
    state = SimpleNamespace(
        ticket_id="T-1", title="x", previous_state="QA",
        stage_iterations={"qa": 2}, current_state="BLOCKED",
    )
    return SimpleNamespace(logs_dir=logs, reports_dir=reports, state=state)


@pytest.fixture(autouse=True)
def _reset_summarizer_state():
    # Reset to disabled between tests so state doesn't leak.
    configure_summarizer(client=None, model="", timeout_seconds=30, enabled=False)
    yield
    configure_summarizer(client=None, model="", timeout_seconds=30, enabled=False)


async def test_summarize_returns_none_when_disabled(tmp_path: Path) -> None:
    client = _FakeClient(response_text="Reason: x\nSuggested action: y")
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=False)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert client.calls == []


async def test_summarize_returns_string_on_success(tmp_path: Path) -> None:
    body = (
        "Reason: Kotlin daemon OOM during ./gradlew testDebugUnitTest.\n"
        "Suggested action: Raise -Xmx; retry the stage once resolved."
    )
    client = _FakeClient(response_text=body)
    configure_summarizer(client=client, model="claude-sonnet-4-6",
                         timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out == body
    # Slice was passed to the LLM.
    assert len(client.calls) == 1
    sent = client.calls[0]
    assert sent["model"] == "claude-sonnet-4-6"
    assert sent["max_tokens"] == 200
    assert sent["temperature"] == 0
    user_content = sent["messages"][0]["content"]
    assert "=== TICKET ===" in user_content
    assert "=== CRASH SIGNALS ===" in user_content


async def test_summarize_returns_none_on_timeout(tmp_path: Path, caplog) -> None:
    import asyncio

    class _SlowClient:
        class _Messages:
            async def create(self, **kw):
                await asyncio.sleep(10)
        messages = _Messages()

    configure_summarizer(client=_SlowClient(), model="m",
                         timeout_seconds=0.05, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert any("timeout" in r.message.lower() for r in caplog.records)


async def test_summarize_returns_none_on_api_error(tmp_path: Path, caplog) -> None:
    client = _FakeClient(response_text=RuntimeError("rate limit"))
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert any("api_error" in r.message.lower() for r in caplog.records)


async def test_summarize_returns_none_on_malformed_output(tmp_path: Path, caplog) -> None:
    client = _FakeClient(response_text="here's what I think happened: stuff broke")
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert any("malformed" in r.message.lower() for r in caplog.records)


async def test_summarize_returns_none_when_too_long(tmp_path: Path, caplog) -> None:
    long_body = (
        "Reason: " + ("x" * 400) + "\nSuggested action: " + ("y" * 200)
    )
    client = _FakeClient(response_text=long_body)
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert any("too_long" in r.message.lower() for r in caplog.records)


async def test_summarize_passes_raw_error_for_agent_failed(tmp_path: Path) -> None:
    body = "Reason: a.\nSuggested action: b."
    client = _FakeClient(response_text=body)
    configure_summarizer(client=client, model="m", timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(
        _ws(tmp_path), "qa", reason_kind="agent_failed", raw_error="exit 137"
    )
    assert out == body
    sent_content = client.calls[0]["messages"][0]["content"]
    assert "raw_error: exit 137" in sent_content


async def test_summarize_returns_none_when_response_content_malformed(
    tmp_path: Path, caplog
) -> None:
    """If the LLM client returns a response with malformed content (e.g.
    non-iterable), summarize_agent_failure logs api_error and returns None."""
    class _BrokenResponse:
        content = 42  # not iterable

    class _BrokenClient:
        class _Messages:
            async def create(self, **kw):
                return _BrokenResponse()
        messages = _Messages()

    configure_summarizer(client=_BrokenClient(), model="m",
                         timeout_seconds=30, enabled=True)
    out = await summarize_agent_failure(_ws(tmp_path), "qa", reason_kind="missing_output")
    assert out is None
    assert any("api_error" in r.message.lower() for r in caplog.records)
