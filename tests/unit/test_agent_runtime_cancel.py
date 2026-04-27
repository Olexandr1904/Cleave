from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.agent_runtime import AgentRuntime


@pytest.fixture
def runtime():
    registry = MagicMock()
    llm = MagicMock()
    return AgentRuntime(registry, llm)


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

    def test_cancel_returns_false_when_not_running(self, runtime):
        result = runtime.cancel("TICKET-1")
        assert result is False

    def test_cancel_kills_process_group_and_unregisters(self, runtime):
        runtime.register_running("TICKET-1", "dev-agent", pid=99999)
        import os
        import signal
        killed = []
        original_killpg = os.killpg
        original_getpgid = os.getpgid
        os.getpgid = lambda pid: pid
        os.killpg = lambda pgid, sig: killed.append((pgid, sig))
        try:
            result = runtime.cancel("TICKET-1")
            assert result is True
            assert killed == [(99999, signal.SIGTERM)]
            assert runtime.get_running("TICKET-1") is None
        finally:
            os.killpg = original_killpg
            os.getpgid = original_getpgid

    def test_cancel_with_pid_zero_returns_true_without_kill(self, runtime):
        """cancel() with pid=0 returns True but doesn't call os.killpg."""
        runtime.register_running("TICKET-1", "dev-agent", pid=0)
        import os
        killed = []
        original_killpg = os.killpg
        os.killpg = lambda pgid, sig: killed.append((pgid, sig))
        try:
            result = runtime.cancel("TICKET-1")
            assert result is True
            assert killed == []
            assert runtime.get_running("TICKET-1") is None
        finally:
            os.killpg = original_killpg

    def test_cancel_handles_process_lookup_error(self, runtime):
        """cancel() handles ProcessLookupError gracefully."""
        runtime.register_running("TICKET-1", "dev-agent", pid=99999)
        import os
        original_killpg = os.killpg
        original_getpgid = os.getpgid
        def mock_killpg(pgid, sig):
            raise ProcessLookupError("No such process")
        os.getpgid = lambda pid: pid
        os.killpg = mock_killpg
        try:
            result = runtime.cancel("TICKET-1")
            assert result is True
            assert runtime.get_running("TICKET-1") is None
        finally:
            os.killpg = original_killpg
            os.getpgid = original_getpgid

    def test_cancel_falls_back_to_kill_on_permission_error(self, runtime):
        """If killpg raises PermissionError, fall back to os.kill on the leader."""
        runtime.register_running("TICKET-1", "dev-agent", pid=99999)
        import os
        import signal
        killed = []
        original_killpg = os.killpg
        original_getpgid = os.getpgid
        original_kill = os.kill
        os.getpgid = lambda pid: pid
        def raise_perm(pgid, sig):
            raise PermissionError("not permitted")
        os.killpg = raise_perm
        os.kill = lambda pid, sig: killed.append((pid, sig))
        try:
            result = runtime.cancel("TICKET-1")
            assert result is True
            assert killed == [(99999, signal.SIGTERM)]
            assert runtime.get_running("TICKET-1") is None
        finally:
            os.killpg = original_killpg
            os.getpgid = original_getpgid
            os.kill = original_kill

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
