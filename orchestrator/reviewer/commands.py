"""Part commands: environments, placeholder expansion, redaction and a bounded subprocess run."""
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

_BASE_ENV_KEYS = ("HOME", "PATH", "LANG", "TMPDIR", "USER")
# A git that reads no host system or global config (credential helpers, askPass, insteadOf) and never prompts.
NO_HOST_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
# Config validation only checks `{name}` placeholders; expand()'s format_map rejects the rest with these.
TEMPLATE_ERRORS = (ValueError, AttributeError, IndexError, TypeError)


def base_env() -> dict[str, str]:
    """The few host variables a part command needs; nothing else leaks into it."""
    return {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}


def part_env(token: str) -> dict[str, str]:
    """base_env() plus the forge token for `gh`, with every interactive prompt off. The command's own git
    reads no host system or global config and runs no hook or fsmonitor (environment config outranks
    .git/config). Nothing else of .git/config (a filter, a helper, http.*) is overridden here: a job writes the
    clone's .git/config and .git/info/attributes back after its session, and a poll runs no readiness during a
    session that changed them."""
    return {
        **base_env(),
        "GH_TOKEN": token,
        "GITHUB_TOKEN": token,
        "GH_PROMPT_DISABLED": "1",
        **NO_HOST_GIT_ENV,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "core.fsmonitor",
        "GIT_CONFIG_VALUE_1": "false",
        "NO_COLOR": "1",
    }


def placeholder_values(names: frozenset[str], **values: str) -> dict[str, str]:
    """The values of exactly `names`: the placeholders config validation accepts for the field being filled."""
    return {name: values[name] for name in names}


def expand(template: list[str], values: dict[str, str]) -> list[str]:
    """Fill `{name}` placeholders in each argument; a placeholder without a value raises ValueError."""
    try:
        return [arg.format_map(values) for arg in template]
    except KeyError as e:
        raise ValueError(f"placeholder {{{e.args[0]}}} has no value") from None


def redact(text: str, secrets: list[str]) -> str:
    """Replace every non-empty secret in text with ***."""
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and anything it spawned (own session, so pgid == pid)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def run_command(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> CommandResult:
    """Run argv in its own session; on timeout kill its process group and report timed_out."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        return CommandResult(returncode=127, stdout="", stderr=f"not found: {argv[0]}", timed_out=False)
    except OSError as e:  # e.g. no exec bit, no shebang
        return CommandResult(returncode=126, stdout="", stderr=f"cannot run: {argv[0]}: {e.strerror}", timed_out=False)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return CommandResult(returncode=-9, stdout="", stderr="", timed_out=True)
    except BaseException:
        _kill_group(proc)
        await proc.wait()
        raise
    return CommandResult(
        returncode=proc.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=False,
    )


def stderr_tail(text: str, limit: int = 200) -> str:
    """The last non-empty line of text, cut to limit characters."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:limit] if lines else ""
