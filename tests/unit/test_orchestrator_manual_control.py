import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from workspace.workspace import Workspace, WorkspaceState


class TestManualControlSkip:
    @pytest.mark.asyncio
    async def test_advance_skips_manual_control(self):
        """Orchestrator should skip workspaces in MANUAL_CONTROL state."""
        ws_state = WorkspaceState(
            ticket_id="T-1", company_id="c", repo_id="r",
            workspace_root="/tmp/test",
            current_state="MANUAL_CONTROL",
            previous_state="DEV",
        )
        ws = MagicMock(spec=Workspace)
        ws.state = ws_state

        # Create minimal orchestrator
        from orchestrator.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        orch._workflow = MagicMock()
        orch._events = None
        orch._mode_handler = None
        orch._notifier = None
        orch._global_config = MagicMock()
        orch._agent_runtime = MagicMock()
        orch._dry_run = False

        # advance_workspace should return without doing anything
        await orch.advance_workspace(ws)
        # Verify no transition was called
        ws.transition.assert_not_called()
