"""Poster part of kind `command`: the repo's own tool posts a review or thread replies, a dry run first."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from config.schemas import POSTER_PLACEHOLDERS, REPLIES_PLACEHOLDERS, PosterPartConfig
from orchestrator.reviewer.commands import (
    TEMPLATE_ERRORS, CommandResult, expand, part_env, placeholder_values, redact, run_command,
)


class PosterError(Exception):
    pass


class CommandPoster:
    """Runs the configured poster commands from the review clone with the forge token. A dry run and its confirm
    run share one argv but the last argument: the dry-run flag, or the confirm flag."""

    def __init__(self, cfg: PosterPartConfig, *, repo: str, token: str, dry_timeout: float = 300.0,
                 confirm_timeout: float = 900.0):
        self.repo = repo
        self._cfg = cfg
        self._token = token
        self._dry_timeout = dry_timeout
        self._confirm_timeout = confirm_timeout

    def __repr__(self) -> str:
        return f"CommandPoster(repo={self.repo!r})"

    async def review(self, clone_dir: Path, values: dict[str, str], *, confirm: bool) -> CommandResult:
        values = placeholder_values(POSTER_PLACEHOLDERS, **values)
        return await self._run(self._cfg.review_command, values, clone_dir, confirm)

    async def replies(self, clone_dir: Path, values: dict[str, str], *, confirm: bool) -> CommandResult:
        values = placeholder_values(REPLIES_PLACEHOLDERS, **values)
        return await self._run(self._cfg.replies_command, values, clone_dir, confirm)

    async def _run(self, template: list[str], values: dict[str, str], clone_dir: Path,
                   confirm: bool) -> CommandResult:
        try:
            argv = expand(template, values)
        except TEMPLATE_ERRORS as e:
            raise PosterError(f"bad command template: {e}") from None
        argv.append(self._cfg.confirm_flag if confirm else self._cfg.dry_run_flag)
        timeout = self._confirm_timeout if confirm else self._dry_timeout
        result = await run_command(argv, cwd=clone_dir, env=part_env(self._token), timeout=timeout)
        return replace(result, stdout=redact(result.stdout, [self._token]),
                       stderr=redact(result.stderr, [self._token]))
