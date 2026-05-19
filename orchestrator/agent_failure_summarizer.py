"""Summarize crashed-agent failures into operator-facing 'Reason / Suggested
action' strings using a short LLM call.

Used by:
- orchestrator.escalation.build_blocked_reason — when a stage runtime output
  is missing (agent crashed before writing its output file).
- orchestrator.pipeline.agent_stage — when an agent run exits with
  result.success is False.

Returns None on any failure mode; callers fall back to their existing
generic message. Never raises.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


SLICE_MAX_BYTES = 6 * 1024
LOG_TAIL_LINES = 50
CRASH_SIGNAL_MAX_ERROR_LINES = 20
CRASH_SIGNAL_MAX_SYSTEM_LINES = 10
CRASH_SIGNAL_MAX_KEYWORD_LINES = 10
from orchestrator.constants import STAGE_RUNTIME_OUTPUT

# Names of the per-stage output files this summarizer reports presence/absence
# for. Derived from the pipeline's STAGE_RUNTIME_OUTPUT so the two stay in sync
# when new stages are added.
KNOWN_OUTPUT_FILES = tuple(STAGE_RUNTIME_OUTPUT.values())

_KEYWORD_RE = re.compile(
    r"Exception|OutOfMemory|BUILD FAILED|killed|timeout", re.IGNORECASE
)
_HEARTBEAT_RE = re.compile(r"\bheartbeat\b", re.IGNORECASE)


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeError):
        return []


def _ticket_section(workspace: Any, stage_id: str, raw_error: str | None) -> str:
    state = workspace.state
    iterations = getattr(state, "stage_iterations", {}) or {}
    lines = [
        f"ticket_id: {getattr(state, 'ticket_id', '?')}",
        f"title: {getattr(state, 'title', None) or '(no title)'}",
        f"failing_stage: {stage_id}",
        f"previous_state: {getattr(state, 'previous_state', None) or '?'}",
        f"iterations[{stage_id}]: {iterations.get(stage_id, 0)}",
    ]
    if raw_error:
        lines.append(f"raw_error: {raw_error[:400]}")
    return "\n".join(lines)


def _stage_history_section(workspace: Any) -> str:
    log = workspace.reports_dir / "pipeline-log.md"
    if not log.exists():
        return "(no pipeline-log.md)"
    lines = _read_lines(log)
    return "\n".join(lines[-10:])


def _output_files_section(workspace: Any) -> str:
    reports = workspace.reports_dir
    out: list[str] = []
    for name in KNOWN_OUTPUT_FILES:
        present = (reports / name).exists()
        out.append(f"{name}: {'present' if present else 'missing'}")
    return "\n".join(out)


def _crash_signals_section(log_lines: list[str]) -> str:
    if not log_lines:
        return "(no log file)"
    errors: list[str] = []
    systems: list[str] = []
    keywords: list[str] = []
    for line in log_lines:
        if "is_error=True" in line:
            errors.append(line)
        if "event=system" in line and not _HEARTBEAT_RE.search(line):
            systems.append(line)
        if _KEYWORD_RE.search(line) and not _HEARTBEAT_RE.search(line):
            keywords.append(line)
    selected = (
        errors[-CRASH_SIGNAL_MAX_ERROR_LINES:]
        + systems[-CRASH_SIGNAL_MAX_SYSTEM_LINES:]
        + keywords[-CRASH_SIGNAL_MAX_KEYWORD_LINES:]
    )
    if not selected:
        return "(no crash signals found)"
    return "\n".join(selected)


def _log_tail_section(log_lines: list[str]) -> str:
    if not log_lines:
        return "(no log file)"
    return "\n".join(log_lines[-LOG_TAIL_LINES:])


def extract_log_slice(
    workspace: Any, stage_id: str, raw_error: str | None
) -> str:
    """Build the structured input slice the summarizer LLM sees.

    Sections are labeled with === HEADER === markers. The crash signals
    section is never truncated; the log tail is head-truncated if the
    total slice exceeds SLICE_MAX_BYTES.
    """
    log_path = workspace.logs_dir / f"{stage_id}-agent.events.log"
    log_lines = _read_lines(log_path) if log_path.exists() else []

    sections = {
        "TICKET": _ticket_section(workspace, stage_id, raw_error),
        "STAGE HISTORY": _stage_history_section(workspace),
        "OUTPUT FILES": _output_files_section(workspace),
        "CRASH SIGNALS": _crash_signals_section(log_lines),
        "LOG TAIL": _log_tail_section(log_lines),
    }

    log_present = bool(log_lines)

    def render(tail_lines: list[str], log_present: bool) -> str:
        sections_copy = dict(sections)
        if not log_present:
            sections_copy["LOG TAIL"] = "(no log file)"
        elif not tail_lines:
            sections_copy["LOG TAIL"] = "(truncated)"
        else:
            sections_copy["LOG TAIL"] = "\n".join(tail_lines)
        return "\n\n".join(
            f"=== {name} ===\n{body}" for name, body in sections_copy.items()
        )

    rendered = render(log_lines[-LOG_TAIL_LINES:] if log_lines else [], log_present)
    if len(rendered.encode("utf-8")) <= SLICE_MAX_BYTES:
        return rendered

    # Head-truncate the LOG TAIL until under cap. CRASH SIGNALS untouched.
    tail = log_lines[-LOG_TAIL_LINES:] if log_lines else []
    while tail:
        tail = tail[1:]
        rendered = render(tail, log_present)
        if len(rendered.encode("utf-8")) <= SLICE_MAX_BYTES:
            return rendered

    final = render([], log_present)
    if len(final.encode("utf-8")) > SLICE_MAX_BYTES:
        logger.warning(
            "agent_failure_summarizer: base sections exceed cap (%d bytes)",
            len(final.encode("utf-8")),
        )
    return final
