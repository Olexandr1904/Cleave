"""Tests for the Button dataclass."""

from integrations.base.notifier import Button


def test_button_fields():
    btn = Button(label="Approve", action="approve:T-1")
    assert btn.label == "Approve"
    assert btn.action == "approve:T-1"


def test_button_action_under_64_bytes():
    """Telegram callback_data limit is 64 bytes."""
    btn = Button(label="Approve", action="approve:ACME-14595")
    assert len(btn.action.encode("utf-8")) <= 64
