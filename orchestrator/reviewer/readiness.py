"""Readiness part of kind `command`: the repo's own tool prints the board and threads snapshots."""
from __future__ import annotations

import json
import re
from pathlib import Path

from config.schemas import STATUS_COMMAND_PLACEHOLDERS, THREADS_COMMAND_PLACEHOLDERS, ReadinessPartConfig
from orchestrator.reviewer.commands import (
    TEMPLATE_ERRORS, expand, part_env, placeholder_values, redact, run_command, stderr_tail,
)
from orchestrator.reviewer.models import BoardSnapshot, SnapshotPR, Turn

_MISSING = object()
_TYPE_NAMES = {int: "int", str: "str", bool: "bool", list: "list", dict: "object"}
_PR_KEYS = {"number", "title", "url", "author", "draft", "threads_truncated", "turns"}
_SHA = re.compile(r"[0-9a-f]{40}")


class ReadinessError(Exception):
    pass


def _load(text: str, label: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        raise ReadinessError(f"{label}: output: not JSON") from None
    except RecursionError:
        raise ReadinessError(f"{label}: output: nested too deeply") from None
    if not isinstance(data, dict):
        raise ReadinessError(f"{label}: output: expected object")
    return data


def _check(value, kind: type, label: str, where: str):
    # bool is an int subclass; a JSON true is never a valid number.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ReadinessError(f"{label}: {where}: expected {_TYPE_NAMES[kind]}")
    return value


def _field(obj: dict, key: str, kind: type, label: str, prefix: str, default=_MISSING):
    if key not in obj:
        if default is _MISSING:
            raise ReadinessError(f"{label}: {prefix}{key}: missing")
        return default
    return _check(obj[key], kind, label, prefix + key)


def _turn(raw, label: str, where: str) -> Turn:
    _check(raw, dict, label, where)
    threads = _field(raw, "threads", list, label, f"{where}.", default=[])
    return Turn(
        login=_field(raw, "login", str, label, f"{where}."),
        action=_field(raw, "action", str, label, f"{where}."),
        threads=tuple(_check(t, str, label, f"{where}.threads[{i}]") for i, t in enumerate(threads)),
        reviewer=_field(raw, "reviewer", str, label, f"{where}.", default=""),
    )


def _pr(raw, label: str, where: str) -> SnapshotPR:
    _check(raw, dict, label, where)
    p = f"{where}."
    number = _field(raw, "number", int, label, p)
    title = _field(raw, "title", str, label, p)
    url = _field(raw, "url", str, label, p)
    author = _field(raw, "author", str, label, p)
    draft = _field(raw, "draft", bool, label, p)
    threads_truncated = _field(raw, "threads_truncated", bool, label, p, default=False)
    turns = _field(raw, "turns", list, label, p)
    return SnapshotPR(
        number=number, title=title, url=url, author=author, draft=draft,
        threads_truncated=threads_truncated,
        turns=tuple(_turn(t, label, f"{where}.turns[{i}]") for i, t in enumerate(turns)),
        extra={k: v for k, v in raw.items() if k not in _PR_KEYS},
    )


def parse_board_snapshot(text: str) -> BoardSnapshot:
    label = "board snapshot"
    data = _load(text, label)
    me = _field(data, "me", str, label, "")
    prs = _field(data, "prs", list, label, "")
    return BoardSnapshot(me=me, prs=tuple(_pr(raw, label, f"prs[{i}]") for i, raw in enumerate(prs)))


def parse_threads_snapshot(text: str) -> dict:
    label = "threads snapshot"
    data = _load(text, label)
    _field(data, "number", int, label, "")
    _field(data, "author", str, label, "")
    if not _SHA.fullmatch(_field(data, "head_sha", str, label, "")):
        raise ReadinessError(f"{label}: head_sha: expected 40 hex")
    _field(data, "threads", list, label, "")
    _field(data, "review_bodies", list, label, "")
    return data


class CommandReadiness:
    """Runs the configured status/threads commands from the review clone with the forge token."""

    def __init__(self, cfg: ReadinessPartConfig, *, repo: str, token: str, timeout: float = 300.0):
        self.repo = repo
        self._cfg = cfg
        self._token = token
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"CommandReadiness(repo={self.repo!r})"

    async def board(self, clone_dir: Path) -> BoardSnapshot:
        values = placeholder_values(STATUS_COMMAND_PLACEHOLDERS, repo=self.repo)
        return parse_board_snapshot(await self._run(self._cfg.status_command, values, clone_dir))

    async def threads(self, clone_dir: Path, pr: int) -> dict:
        values = placeholder_values(THREADS_COMMAND_PLACEHOLDERS, repo=self.repo, pr=str(pr))
        return parse_threads_snapshot(await self._run(self._cfg.threads_command, values, clone_dir))

    async def _run(self, template: list[str], values: dict[str, str], clone_dir: Path) -> str:
        try:
            argv = expand(template, values)
        except TEMPLATE_ERRORS as e:
            raise ReadinessError(f"bad command template: {e}") from None
        result = await run_command(argv, cwd=clone_dir, env=part_env(self._token), timeout=self._timeout)
        if result.timed_out:
            raise ReadinessError(f"timed out after {int(self._timeout)} s")
        if result.returncode != 0:
            # Redact before cutting, so a token straddling the cut cannot leak in part.
            raise ReadinessError(f"exit {result.returncode}: {stderr_tail(redact(result.stderr, [self._token]))}")
        return result.stdout
