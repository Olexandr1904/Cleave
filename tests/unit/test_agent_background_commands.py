"""A command an agent left running in the background dies with the agent.

A QA agent started a Gradle chain in the background and lost its connection to the API. The daemon killed the
CLI ten minutes later; the chain ran on for twenty more, building beside the next ticket's QA on a host with
memory for one build. Claude Code makes each shell command the leader of a process group of its own, and the
kill was of the CLI's group only.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from integrations.llm.claude_code_adapter import ClaudeCodeAdapter, descendant_groups
from orchestrator.agent_runtime import AgentRuntime

# What the CLI does with a shell command: starts it as the leader of a new process group.
START_A_BACKGROUND_COMMAND = (
    "import subprocess\n"
    "background = subprocess.Popen(['sleep', '300'], start_new_session=True)\n"
)


def _running(pid: int) -> bool:
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


async def _stops(pid: int) -> bool:
    for _ in range(60):
        if not _running(pid):
            return True
        await asyncio.sleep(0.05)
    return False


def _kill(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


async def test_the_command_of_an_agent_killed_after_its_result_is_killed_with_it(tmp_path: Path):
    """The CLI printed its result and stayed, its background command still running: the adapter kills it once
    the idle grace is over."""
    cli = tmp_path / "claude"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        + START_A_BACKGROUND_COMMAND +
        "open('background.pid', 'w').write(str(background.pid))\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done',"
        " 'usage': {}}), flush=True)\n"
        "time.sleep(300)\n"
    )
    cli.chmod(0o755)
    adapter = ClaudeCodeAdapter(model_provider=lambda: "claude-sonnet-4-5")
    adapter._claude_bin = str(cli)

    try:
        with patch("integrations.llm.claude_code_adapter.POST_RESULT_IDLE_GRACE_SECONDS", 0.3), \
             patch("integrations.llm.claude_code_adapter._WATCHDOG_POLL_SECONDS", 0.05):
            response = await asyncio.wait_for(
                adapter.execute_in_workspace(prompt="go", cwd=str(tmp_path), allowed_tools=["read_file"]),
                timeout=20,
            )
        background = int((tmp_path / "background.pid").read_text())

        assert response.content == "done"
        assert await _stops(background)
    finally:
        pid_file = tmp_path / "background.pid"
        if pid_file.exists():
            _kill(int(pid_file.read_text()))


async def test_the_command_of_a_cancelled_agent_is_killed_with_it():
    agent = subprocess.Popen(
        [sys.executable, "-c",
         START_A_BACKGROUND_COMMAND + "import time\nprint(background.pid, flush=True)\ntime.sleep(300)\n"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    background = int(agent.stdout.readline())
    runtime = AgentRuntime(MagicMock(), MagicMock())
    runtime.register_running("ACME-1", "qa-agent", pid=agent.pid)
    # The adapter reaps the CLI it started; here the test does, or the dead agent stays a zombie that cancel()
    # takes for alive.
    reaped = asyncio.get_running_loop().run_in_executor(None, agent.wait)

    try:
        assert await runtime.cancel("ACME-1") is True

        assert await asyncio.wait_for(reaped, timeout=5) is not None
        assert await _stops(background)
    finally:
        _kill(background)
        agent.kill()


def test_only_a_process_this_one_started_has_descendants_to_kill():
    """The groups are killed with no further question, so they are taken from our own child only: not from a
    pid that has passed to some other process, and not from a made-up pid in a test."""
    assert descendant_groups(1) == set()  # launchd: everything descends from it
    assert descendant_groups(os.getppid()) == set()
