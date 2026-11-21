# tests/unit/test_skillflow_build_slot.py
from __future__ import annotations

import asyncio
import json
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from orchestrator.skillflow import build_slot
from orchestrator.skillflow.build_slot import BuildSlotServer, build_tools, hook_command, is_build

SCRIPT = Path(build_slot.__file__).absolute()


@pytest.fixture
def short_dir():
    """A directory whose socket path fits the 104 bytes macOS allows (pytest's tmp_path does not)."""
    path = Path(tempfile.mkdtemp(prefix="cs-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def _until(condition, tries=300):
    for _ in range(tries):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def _hook(payload, sock="/tmp/x.sock", tools=("gradlew",)) -> str:
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "hook", sock, *tools],
                            input=payload if isinstance(payload, str) else json.dumps(payload),
                            capture_output=True, text=True, check=True)
    return result.stdout


# --- which commands are builds ---

@pytest.mark.parametrize("commands, tools", [
    (["./gradlew test", "./gradlew detekt", "./gradlew assembleDebug"], ["gradlew"]),
    (["cd android && ./gradlew test"], ["gradlew"]),
    (["(cd android; ./gradlew lint) | tee lint.log"], ["gradlew"]),
    (["npm test", "npm run lint", "npx tsc --noEmit"], ["npm", "npx"]),
    (["CI=1 env TZ=UTC make check"], ["make"]),
    (["nice -n 10 ./gradlew test", "timeout 600 ./gradlew lint"], ["gradlew"]),
    (["if [ -f gradlew ]; then ./gradlew lint; fi"], ["gradlew"]),
    (["", "  "], []),
])
def test_the_build_tools_are_the_programs_the_gate_commands_start(commands, tools):
    assert build_tools(commands) == tools

@pytest.mark.parametrize("command, built", [
    ("./gradlew :app:testDebugUnitTest --tests LoginTest", True),
    ("cd app && ../gradlew assembleDebug 2>&1 | tail -20", True),
    ("timeout 600 ./gradlew test", True),
    ("JAVA_HOME=/jdk ./gradlew build", True),
    ('bash -c "./gradlew test"', True),
    ("git status --porcelain", False),
    ("cat build.gradle.kts", False),
    ("ls gradle/wrapper", False),
])
def test_a_build_is_a_command_that_names_a_build_tool(command, built):
    assert is_build(command, ["gradlew"]) is built


# --- the PreToolUse hook ---

def test_the_hook_runs_a_build_behind_the_slot_wait_keeping_every_other_field():
    tool_input = {"command": "./gradlew test", "description": "Run the tests", "timeout": 600000,
                  "run_in_background": True}
    out = json.loads(_hook({"tool_name": "Bash", "tool_input": tool_input}, sock="/tmp/cs 1/slot.sock"))
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse" and "permissionDecision" not in specific
    updated = specific["updatedInput"]
    assert {k: v for k, v in updated.items() if k != "command"} == {k: v for k, v in tool_input.items()
                                                                    if k != "command"}
    wait, original = updated["command"].split("; ", 1)
    assert original == "./gradlew test"
    assert wait == f"{sys.executable} -I {SCRIPT} wait '/tmp/cs 1/slot.sock'"

@pytest.mark.parametrize("payload", [
    {"tool_name": "Bash", "tool_input": {"command": "git status"}},
    {"tool_name": "Read", "tool_input": {"file_path": "gradlew"}},
    {"tool_name": "Bash", "tool_input": {"command": ["./gradlew"]}},
    {"tool_name": "Bash"},
    "not json",
])
def test_the_hook_leaves_every_other_call_as_it_is(payload):
    assert _hook(payload) == ""

def test_hook_command_names_the_socket_and_the_tools():
    command = hook_command(Path("/tmp/cs 1/slot.sock"), ["gradlew", "npm"])
    assert command == f"{sys.executable} -I {SCRIPT} hook '/tmp/cs 1/slot.sock' gradlew npm"


# --- the slot, taken over the socket ---

async def _shell(script: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec("/bin/sh", "-c", script, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)

def _wait(path: Path) -> str:
    return f"{sys.executable} -I {SCRIPT} wait {path}"

async def test_a_command_holds_the_slot_while_its_shell_lives(short_dir):
    slot, path = asyncio.Semaphore(1), short_dir / "slot.sock"
    server = BuildSlotServer(slot, path)
    assert await server.start() is True
    try:
        marker = short_dir / "ran"
        shell = await _shell(f"{_wait(path)}; touch {marker}; sleep 0.5")
        assert await _until(lambda: marker.exists())
        assert slot.locked()                               # the command runs in the slot
        out, err = await shell.communicate()
        assert shell.returncode == 0 and err == b""
        assert await _until(lambda: not slot.locked())     # given back once the shell is gone
    finally:
        server.close()

async def test_a_second_build_waits_and_says_so(short_dir):
    slot, path = asyncio.Semaphore(1), short_dir / "slot.sock"
    server = BuildSlotServer(slot, path)
    await server.start()
    try:
        await slot.acquire()                               # a staged build holds it
        marker = short_dir / "ran"
        shell = await _shell(f"{_wait(path)}; touch {marker}")
        await asyncio.sleep(0.3)
        assert not marker.exists()
        slot.release()
        out, err = await shell.communicate()
        assert marker.exists() and b"waiting for the host's build slot" in err
        assert await _until(lambda: not slot.locked())
    finally:
        server.close()

async def test_a_killed_command_gives_the_slot_back(short_dir):
    slot, path = asyncio.Semaphore(1), short_dir / "slot.sock"
    server = BuildSlotServer(slot, path)
    await server.start()
    try:
        shell = await _shell(f"{_wait(path)}; sleep 30")
        assert await _until(slot.locked)
        shell.send_signal(signal.SIGKILL)
        await shell.wait()
        assert await _until(lambda: not slot.locked())
    finally:
        server.close()

async def test_a_command_killed_while_it_waits_never_takes_the_slot(short_dir):
    slot, path = asyncio.Semaphore(1), short_dir / "slot.sock"
    server = BuildSlotServer(slot, path)
    await server.start()
    try:
        await slot.acquire()
        client = await asyncio.create_subprocess_exec(sys.executable, "-I", str(SCRIPT), "wait", str(path),
                                                      stderr=asyncio.subprocess.PIPE)
        assert b"waiting" in await client.stderr.readline()
        client.kill(); await client.wait()
        await asyncio.sleep(0.1)
        slot.release()
        assert await _until(lambda: not slot.locked()) and not slot.locked()
        await asyncio.sleep(0.2)
        assert not slot.locked()                           # nobody took it on the dead client's behalf
    finally:
        server.close()

async def test_without_the_daemon_the_command_runs_all_the_same(short_dir):
    shell = await _shell(f"{_wait(short_dir / 'none.sock')}; echo built")
    out, err = await shell.communicate()
    assert shell.returncode == 0 and out == b"built\n" and b"build slot not taken" in err

async def test_the_server_replaces_a_socket_left_by_an_earlier_daemon(short_dir):
    path = short_dir / "slot.sock"
    stale = socket.socket(socket.AF_UNIX); stale.bind(str(path)); stale.close()   # a killed daemon's
    slot = asyncio.Semaphore(1)
    server = BuildSlotServer(slot, path)
    assert await server.start() is True
    try:
        shell = await _shell(f"{_wait(path)}; echo built")
        out, err = await shell.communicate()
        assert out == b"built\n" and err == b"" and await _until(lambda: not slot.locked())
    finally:
        server.close()
    assert not path.exists()

async def test_the_server_never_takes_a_path_that_is_not_a_socket(short_dir):
    path = short_dir / "slot.sock"
    path.write_text("keep")
    assert await BuildSlotServer(asyncio.Semaphore(1), path).start() is False
    assert path.read_text() == "keep"

async def test_a_socket_path_too_long_is_not_served(tmp_path):
    assert await BuildSlotServer(asyncio.Semaphore(1), tmp_path / ("x" * 120) / "slot.sock").start() is False

def test_the_socket_is_named_by_its_absolute_path():
    # The wait runs in the session's shell, which works in another directory than the daemon.
    assert BuildSlotServer(asyncio.Semaphore(1), Path("build-slot.sock")).path == Path.cwd() / "build-slot.sock"
