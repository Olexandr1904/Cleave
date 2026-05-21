from __future__ import annotations

from types import SimpleNamespace

from config.schemas import GlobalConfig
from orchestrator.runtime import Runtime, is_build_stage
from workspace.workspace import Stage

# Fully synchronous, loop-independent tests: the build-stage decision is a pure
# function and the build semaphore's size is checked on the constructed object,
# so these survive the suite's pre-existing async event-loop pollution. The
# serialization itself is guaranteed by asyncio.Semaphore given this wiring.


def _make_runtime(*, max_parallel_builds=1, max_parallel_tickets=7):
    cfg = GlobalConfig()
    cfg.defaults.max_parallel_builds = max_parallel_builds
    cfg.defaults.max_parallel_tickets = max_parallel_tickets

    async def _noop_poll():
        return None

    return Runtime(
        cfg,
        SimpleNamespace(),  # workspace_manager unused here
        poll_callback=_noop_poll,
        advance_callback=lambda ws: None,
    )


def test_only_dev_and_qa_are_build_stages():
    assert is_build_stage(Stage.DEV) is True
    assert is_build_stage(Stage.QA) is True
    for s in (Stage.ANALYSIS, Stage.SCOPE_CHECK, Stage.PUSHED,
              Stage.PR_REVIEW, Stage.AWAITING_APPROVAL):
        assert is_build_stage(s) is False


def test_build_semaphore_sized_from_config():
    assert _make_runtime(max_parallel_builds=2)._build_semaphore._value == 2
    assert _make_runtime(max_parallel_builds=1)._build_semaphore._value == 1


def test_build_semaphore_floor_is_one():
    # A bogus 0/negative config must not deadlock every build forever.
    assert _make_runtime(max_parallel_builds=0)._build_semaphore._value == 1


def test_build_semaphore_independent_of_general_semaphore():
    rt = _make_runtime(max_parallel_builds=1, max_parallel_tickets=7)
    assert rt._build_semaphore._value == 1
    assert rt._agent_semaphore._value == 7
