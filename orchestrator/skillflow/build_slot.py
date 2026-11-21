"""The build slot for a skill session's own builds. A session turn holds no slot: a PreToolUse hook in Cleave's
settings file for the session puts a wait for the slot in front of every Bash command that runs a build, and the
daemon takes the pipeline's build slot (the asyncio semaphore the staged flow's build stages and Cleave's gates
take) on that command's behalf while the command's shell lives.

A build is a command that names a program one of the repo's gate commands starts (`gradlew` for `./gradlew test`).
This is resource scheduling, not a guard: a command that runs a build under another name, or a session that edits
the hook out, only costs memory.

The file runs as a script too, with the Python of the daemon and nothing but the standard library (`python -I`):
`hook <socket> <tool>…` is the hook (the PreToolUse input on stdin, its output on stdout), and `wait <socket>`
the wait in front of a build. The wait asks the daemon over its Unix socket, and once the slot is given leaves a
process behind that keeps the connection open until the command's shell is gone; the daemon gives the slot back
when the connection closes, so a killed command gives it back too. Whatever fails on the way, the command runs
all the same."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import select
import shlex
import socket
import stat
import sys
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

SCRIPT = Path(__file__).absolute()
HOOK_TIMEOUT = 10                  # seconds: the hook only reads its input and answers
_POLL = 0.5                        # seconds between two looks at the command's shell
_WAITING = "Cleave: waiting for the host's build slot: another build is running."
# What separates words in a command; and the parts of a command list, each of which starts a program.
_WORD_BREAK = re.compile(r"[\s;&|()<>'\"`]+")
_LIST_BREAK = re.compile(r"&&|\|\||[;\n]")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# An option or a number (a prefix's own, as in `nice -n 10` or `timeout 600`): no program.
_OPTION = re.compile(r"[-\d]")
_PREFIXES = frozenset({"env", "exec", "command", "nice", "nohup", "time", "timeout"})   # run the program after them
_KEYWORDS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{"})   # a command follows them
# No build tool, and nothing after them in the part is one: builtins, and the shell words no command follows.
_BUILTINS = frozenset({"cd", "pushd", "popd", "export", "set", "unset", "source", ".", "echo", "true", "false",
                       ":", "test", "[", "exit", "for", "case", "select", "function", "in", "fi", "done", "esac",
                       "}"})


def build_tools(commands: Iterable[str]) -> list[str]:
    """The programs the gate commands start, by file name: each part of a command list starts one, after its
    variable assignments, env-like prefixes with their options and shell keywords (if, then, …); a shell builtin
    (cd, export, …) is no build tool."""
    tools = set()
    for command in commands:
        for part in _LIST_BREAK.split(command):
            for word in part.replace("(", " ").replace(")", " ").split():
                if _ASSIGNMENT.match(word) or word in _PREFIXES or word in _KEYWORDS or _OPTION.match(word):
                    continue
                if (name := word.rsplit("/", 1)[-1]) not in _BUILTINS:
                    tools.add(name)
                break
    return sorted(tools)


def is_build(command: str, tools: Iterable[str]) -> bool:
    """Whether any word of the command names one of the build tools (by file name)."""
    names = set(tools)
    return any(word.rsplit("/", 1)[-1] in names for word in _WORD_BREAK.split(command) if word)


def hook_command(socket_path: Path, tools: list[str]) -> str:
    """The hook's command line for Cleave's settings file."""
    return shlex.join([sys.executable, "-I", str(SCRIPT), "hook", str(socket_path), *tools])


def _wait_command(socket_path: str) -> str:
    return shlex.join([sys.executable, "-I", str(SCRIPT), "wait", socket_path])


class BuildSlotServer:
    """The daemon's end: on each connection it takes `slot` for the client and gives it back when the connection
    closes. `wait\\n` goes out first when the slot is taken, `go\\n` once the client has it. A client gone while
    it waits never gets it."""

    def __init__(self, slot: asyncio.Semaphore, path: Path):
        self._slot = slot
        self.path = Path(path).absolute()   # the session's shell runs elsewhere
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> bool:
        """Listen at path, only for this user; a socket left there by an earlier daemon is replaced, anything
        else at the path is left as it is. False, with a warning, when it cannot listen."""
        try:
            if self.path.is_symlink() or self.path.exists():
                if not stat.S_ISSOCK(self.path.lstat().st_mode):
                    logger.warning("Build slot for skill sessions not served: %s is not a socket", self.path)
                    return False
                self.path.unlink()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
            self.path.chmod(0o600)
        except OSError as e:   # a path too long for a socket, a directory it cannot write
            logger.warning("Build slot for skill sessions not served at %s: %s", self.path, e)
            return False
        return True

    @property
    def serving(self) -> bool:
        return self._server is not None

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
            self.path.unlink(missing_ok=True)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        acquire = gone = None
        try:
            if self._slot.locked():
                writer.write(b"wait\n")
                await writer.drain()
            acquire = asyncio.ensure_future(self._slot.acquire())
            gone = asyncio.ensure_future(reader.read())   # the client sends nothing: this ends when it closes
            await asyncio.wait({acquire, gone}, return_when=asyncio.FIRST_COMPLETED)
            if not gone.done():
                writer.write(b"go\n")
                await writer.drain()
                await gone
        except OSError:   # the client went away while we wrote to it
            pass
        finally:
            if gone is not None:
                if gone.done() and not gone.cancelled():
                    gone.exception()   # read: a reset connection's error is no "never retrieved" in the log
                else:
                    gone.cancel()
            if acquire is not None:
                if acquire.done() and not acquire.cancelled():
                    self._slot.release()
                else:
                    acquire.cancel()
            writer.close()


def rewrite(payload: dict, socket_path: str, tools: list[str]) -> dict | None:
    """The hook's answer to one PreToolUse input: a Bash build gets the wait in front of its command, every other
    field as it was; anything else gets none. It decides nothing: the call goes through the permission rules as
    the rewritten command."""
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not is_build(command, tools):
        return None
    # The command itself is left as it is, in the session's own shell: the permission rules still read it, and a
    # cd in it still carries over.
    updated = {**tool_input, "command": f"{_wait_command(socket_path)}; {command}"}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}


def _hook(socket_path: str, tools: list[str]) -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if isinstance(payload, dict) and (answer := rewrite(payload, socket_path, tools)) is not None:
        print(json.dumps(answer))
    return 0


def _wait(socket_path: str) -> int:
    """Take the slot for the shell that runs this, then leave it to a process of its own; 0 whatever happens."""
    shell = os.getppid()
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(socket_path)
        replies = conn.makefile("rb")
        while (line := replies.readline()) != b"go\n":
            if not line:
                raise ConnectionError("the daemon closed the connection")
            if line == b"wait\n":
                print(_WAITING, file=sys.stderr, flush=True)
    except OSError as e:
        print(f"Cleave: build slot not taken ({e}); the command runs without it.", file=sys.stderr)
        return 0
    if os.fork() == 0:
        _hold(conn, shell)
    return 0


def _hold(conn: socket.socket, shell: int) -> None:
    """The slot's holder: until the shell is gone (or the daemon), with no output of its own. Never returns."""
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):   # nothing of the command's output stays open in here
            os.dup2(devnull, fd)
        while _alive(shell):
            readable, _, _ = select.select([conn], [], [], _POLL)
            if readable and not conn.recv(1):   # the daemon is gone
                break
    finally:
        os._exit(0)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:   # gone, or a process of another user's under that number by now
        return False
    return True


def main(argv: list[str]) -> int:
    if argv[:1] == ["hook"] and len(argv) >= 2:
        return _hook(argv[1], argv[2:])
    if argv[:1] == ["wait"] and len(argv) == 2:
        return _wait(argv[1])
    print("usage: build_slot.py hook <socket> <tool>… | wait <socket>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
