from __future__ import annotations

import os
import signal
from unittest.mock import MagicMock

import pytest

from orchestrator.agent_runtime import AgentRuntime


@pytest.fixture
def runtime():
    registry = MagicMock()
    llm = MagicMock()
    return AgentRuntime(registry, llm)


@pytest.fixture
def patch_os(monkeypatch):
    """Patch the OS surface used by cancel() and return a recorder.

    The recorder lets each test program exactly when the simulated process
    dies (initially alive; flip to dead via `recorder.dead = True`).
    """

    class Recorder:
        def __init__(self) -> None:
            self.signals_sent: list[tuple[int, int]] = []
            self.dead = False

        def killpg(self, pgid: int, sig: int) -> None:
            self.signals_sent.append((pgid, sig))
            if sig == signal.SIGKILL:
                self.dead = True  # SIGKILL is unstoppable

        def kill(self, pid: int, sig: int) -> None:
            if sig == 0:  # liveness check
                if self.dead:
                    raise ProcessLookupError("dead")
                return
            self.signals_sent.append((pid, sig))
            if sig == signal.SIGKILL:
                self.dead = True

        def getpgid(self, pid: int) -> int:
            return pid

    rec = Recorder()
    monkeypatch.setattr(os, "killpg", rec.killpg)
    monkeypatch.setattr(os, "kill", rec.kill)
    monkeypatch.setattr(os, "getpgid", rec.getpgid)
    return rec


class TestAgentCancel:
    def test_register_running_agent(self, runtime):
        runtime.register_running("TICKET-1", "dev-agent", pid=12345)
        info = runtime.get_running("TICKET-1")
        assert info is not None
        assert info["agent_id"] == "dev-agent"
        assert info["pid"] == 12345

    def test_get_running_returns_none_when_not_running(self, runtime):
        assert runtime.get_running("TICKET-1") is None

    def test_unregister_running_agent(self, runtime):
        runtime.register_running("TICKET-1", "dev-agent", pid=12345)
        runtime.unregister_running("TICKET-1")
        assert runtime.get_running("TICKET-1") is None

    @pytest.mark.asyncio
    async def test_cancel_returns_false_when_not_running(self, runtime):
        result = await runtime.cancel("TICKET-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_sigterm_succeeds_when_process_exits_quickly(self, runtime, patch_os):
        """Process responds to SIGTERM before SIGKILL escalation kicks in."""
        runtime.register_running("TICKET-1", "dev-agent", pid=99999)
        # Simulate the process dying shortly after SIGTERM by flipping the flag
        # before the first poll completes.
        original_killpg = patch_os.killpg

        def killpg_then_die(pgid, sig):
            original_killpg(pgid, sig)
            patch_os.dead = True
        patch_os.killpg = killpg_then_die
        import os as _os
        _os.killpg = killpg_then_die

        result = await runtime.cancel("TICKET-1", sigkill_after=1.0)
        assert result is True
        assert (99999, signal.SIGTERM) in patch_os.signals_sent
        assert (99999, signal.SIGKILL) not in patch_os.signals_sent
        assert runtime.get_running("TICKET-1") is None

    @pytest.mark.asyncio
    async def test_cancel_escalates_to_sigkill_when_sigterm_ignored(self, runtime, patch_os):
        """Process ignores SIGTERM → escalation to SIGKILL after the deadline."""
        runtime.register_running("TICKET-1", "dev-agent", pid=99999)
        # patch_os.dead stays False after SIGTERM, so liveness keeps reporting
        # alive until SIGKILL flips it.
        result = await runtime.cancel("TICKET-1", sigkill_after=0.05)
        assert result is True
        sigs = [s for (_, s) in patch_os.signals_sent]
        assert signal.SIGTERM in sigs
        assert signal.SIGKILL in sigs
        # SIGTERM came first
        assert sigs.index(signal.SIGTERM) < sigs.index(signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_cancel_with_pid_zero_returns_true_without_kill(self, runtime, patch_os):
        runtime.register_running("TICKET-1", "dev-agent", pid=0)
        result = await runtime.cancel("TICKET-1")
        assert result is True
        assert patch_os.signals_sent == []
        assert runtime.get_running("TICKET-1") is None

    @pytest.mark.asyncio
    async def test_cancel_handles_already_gone_process(self, runtime, monkeypatch):
        """If the process is gone before SIGTERM, cancel still returns True."""
        runtime.register_running("TICKET-1", "dev-agent", pid=99999)

        def gone_getpgid(pid):
            raise ProcessLookupError("gone")
        monkeypatch.setattr(os, "getpgid", gone_getpgid)

        result = await runtime.cancel("TICKET-1")
        assert result is True
        assert runtime.get_running("TICKET-1") is None

    @pytest.mark.asyncio
    async def test_cancel_falls_back_to_kill_on_permission_error(self, runtime, monkeypatch):
        """If killpg raises PermissionError, fall back to os.kill on the leader."""
        runtime.register_running("TICKET-1", "dev-agent", pid=99999)
        leader_signals = []
        dead = [False]

        def fake_killpg(pgid, sig):
            raise PermissionError("not permitted")

        def fake_kill(pid, sig):
            if sig == 0:
                if dead[0]:
                    raise ProcessLookupError("dead")
                return
            leader_signals.append((pid, sig))
            dead[0] = True  # die immediately on real signal so test is fast

        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(os, "killpg", fake_killpg)
        monkeypatch.setattr(os, "kill", fake_kill)

        result = await runtime.cancel("TICKET-1", sigkill_after=1.0)
        assert result is True
        assert (99999, signal.SIGTERM) in leader_signals
        assert runtime.get_running("TICKET-1") is None

    def test_update_pid_sets_pid_on_running_entry(self, runtime):
        runtime.register_running("TICKET-1", "dev-agent", pid=0)
        runtime.update_pid("TICKET-1", 54321)
        info = runtime.get_running("TICKET-1")
        assert info["pid"] == 54321

    def test_update_pid_no_op_when_not_running(self, runtime):
        runtime.update_pid("TICKET-NOPE", 54321)
        assert runtime.get_running("TICKET-NOPE") is None

    def test_register_running_sets_started_at(self, runtime):
        """register_running stores a started_at timestamp."""
        import time
        before = time.time()
        runtime.register_running("TICKET-1", "dev-agent", pid=123)
        after = time.time()
        info = runtime.get_running("TICKET-1")
        assert info["started_at"] >= before
        assert info["started_at"] <= after
