"""Verify the tracker port exposes the new comment/history/attachment methods."""
import inspect
from integrations.base.tracker import TicketComment, StatusChange, TrackerInterface


def test_new_dataclasses_present() -> None:
    c = TicketComment(id="1", author="a", created="2026-05-11", body="hi")
    assert c.body == "hi"
    s = StatusChange(
        created="2026-05-11", from_status="A", to_status="B", author="a",
    )
    assert s.to_status == "B"


def test_new_abstract_methods_declared() -> None:
    abstract = TrackerInterface.__abstractmethods__
    for name in (
        "get_comments", "get_status_history",
        "download_attachment", "list_transitions",
    ):
        assert name in abstract, f"missing abstract method: {name}"


def test_methods_are_async() -> None:
    for name in (
        "get_comments", "get_status_history",
        "download_attachment", "list_transitions",
    ):
        method = getattr(TrackerInterface, name)
        assert inspect.iscoroutinefunction(method), f"{name} must be async"
