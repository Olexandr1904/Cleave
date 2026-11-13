"""Part commands: environments, placeholder expansion, redaction and a bounded subprocess run."""
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

_BASE_ENV_KEYS = ("HOME", "PATH", "LANG", "TMPDIR", "USER")


def base_env() -> dict[str, str]:
    """The few host variables a part command needs; nothing else leaks into it."""
    return {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}


def part_env(token: str) -> dict[str, str]:
    """base_env() plus the forge token for `gh`, with every interactive prompt off."""
    return {
        **base_env(),
        "GH_TOKEN": token,
        "GITHUB_TOKEN": token,
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "NO_COLOR": "1",
    }


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
