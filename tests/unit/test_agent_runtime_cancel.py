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

    def test_cancel_kills_process_and_unregisters(self, runtime):
        runtime.register_running("TICKET-1", "dev-agent", pid=99999)
        # Mock os.kill to avoid actually killing a process
        import os
        import signal
        original_kill = os.kill
        killed = []
        def mock_kill(pid, sig):
            killed.append((pid, sig))
        os.kill = mock_kill
        try:
            result = runtime.cancel("TICKET-1")
            assert result is True
            assert killed == [(99999, signal.SIGTERM)]
            assert runtime.get_running("TICKET-1") is None
        finally:
            os.kill = original_kill
