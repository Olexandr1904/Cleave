"""One turn of the skill session: the `claude` run that works a ticket with the repo's own skill — its scrubbed
environment with the build's variables, Cleave's own settings file, and how the turn ended."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config.schemas import SkillFlowConfig
from health.validators import CLAUDE_ENV_KEYS
from integrations.llm.claude_code_adapter import BASH_TIMEOUT_DEFAULTS
from orchestrator.agent_runtime import HARD_SAFETY_RULES
from orchestrator.reviewer.commands import NO_HOST_GIT_ENV
from orchestrator.reviewer.session import _outcome, _secret_rules

SKILL_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill", "Agent", "TodoWrite")
SKILL_MAX_TURNS = 1000            # the wall clock is the real limit
# No sandbox bounds this session (it builds with the host's toolchain), so these rules carry the refusals: no forge
# or network client, no keychain, no git that reaches or rewires a remote or a credential (Cleave's own git calls
# fetch and push), no read of the host's credential files.
DENY = (
    "Bash(gh:*)", "Bash(glab:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(security:*)",
    "Bash(git push:*)", "Bash(git fetch:*)", "Bash(git pull:*)", "Bash(git ls-remote:*)",
    "Bash(git remote:*)", "Bash(git config:*)", "Bash(git credential:*)",
    "Read(~/.git-credentials)", "Read(~/.netrc)", "Read(~/.config/gh/**)",
)
# The CLI's wording when --resume names a session id it does not know: the cut turn ended before the CLI made the
# session, so it starts cold. Any other failure of that resume may leave a session that holds work.
_NO_SESSION = "no conversation found"


class MissingBuildVariable(Exception):
    """A build_env variable the host does not set; str(e) is its name."""


@dataclass(frozen=True)
class TurnOutcome:
    kind: str                     # "ok" | "quota" | "login" | "failed"
    text: str = ""
    error: str = ""
    retry_at: datetime | None = None
    cost_usd: float = 0.0
    turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    no_session: bool = False      # the CLI knows no session of that id


def skill_env(
    environ: Mapping[str, str], build_env: list[str], author_name: str, author_email: str,
) -> dict[str, str]:
    """The session's whole environment: a few host variables, the repo's build variables, git without prompts or
    host config and with Cleave's author, and the Bash timeouts a build needs — no forge, tracker, Telegram or
    Claude API secret."""
    for name in build_env:
        if name not in environ:
            raise MissingBuildVariable(name)
    return {
        **{k: environ[k] for k in CLAUDE_ENV_KEYS if k in environ},
        **{name: environ[name] for name in build_env},
        # Cleave's own variables come last: a build variable never brings back the host's git config, a prompt or
        # another author.
        **NO_HOST_GIT_ENV,
        "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
        **BASH_TIMEOUT_DEFAULTS,
    }


def skill_settings_json(secret_paths: list[Path]) -> dict:
    """Cleave's own settings file for the session: the DENY rules and a Read deny for each secret. No sandbox: the
    session builds and needs the host's toolchain."""
    return {"permissions": {"deny": [*DENY, *(f"Read({s})" for s in _secret_rules(secret_paths))]}}


async def run_turn(
    llm, *, checkout_dir: Path, session_dir: Path, prompt: str, session_id: str, resume: bool, model: str,
    cfg: SkillFlowConfig, secret_paths: list[Path], env: dict[str, str], turn: int,
) -> TurnOutcome:
    """Run one turn of the skill session and classify how it ended; never raises for a failed run. A failed
    --resume fails the turn: a cold start would drop the session's work so far. --strict-mcp-config without
    --mcp-config: no MCP server configured on the host loads."""
    session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings = session_dir / "settings.json"
    settings.write_text(json.dumps(skill_settings_json(secret_paths), indent=2) + "\n", encoding="utf-8")
    try:
        response = await llm.execute_in_workspace(
            prompt=prompt,
            cwd=str(checkout_dir),
            model=model,
            effort=cfg.effort,
            timeout=cfg.timeout_seconds,
            max_turns=SKILL_MAX_TURNS,
            system=HARD_SAFETY_RULES,
            env=env,
            session_id=session_id,
            resume=resume,
            resume_fallback=False,
            raw_allowed_tools=list(SKILL_TOOLS),
            extra_args=["--settings", str(settings), "--strict-mcp-config"],
            progress_log_path=session_dir / "progress.log",
            raw_stream_path=session_dir / f"stream-{turn}.jsonl",
        )
    except Exception as e:
        o = _outcome(e)
        # Read from the whole text: the error is cut to one short line, and the CLI's words may sit past the cut.
        # A CLIError keeps the CLI's own error text apart, and it is not always in str(e).
        texts = (str(e), str(getattr(e, "result_error", "") or ""))
        return TurnOutcome(o.kind, error=o.error, retry_at=o.retry_at, cost_usd=o.cost_usd, turns=o.turns,
                           no_session=any(_NO_SESSION in text.casefold() for text in texts))
    return TurnOutcome("ok", text=response.content, cost_usd=response.cost_usd, turns=response.num_turns,
                       tokens_in=response.input_tokens, tokens_out=response.output_tokens)
