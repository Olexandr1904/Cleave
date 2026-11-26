"""Quiet hours: the nightly window in which the bot's messages go out silent (Telegram's disable_notification).

One global setting, host local time, on by default from 22:00 to 09:00. It is stored in the dashboard's settings
table (dashboard/settings_store.py) and set in Settings on the dashboard; TelegramAdapter reads it on each send."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import time

_HH_MM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


@dataclass
class QuietHours:
    enabled: bool = True
    start: str = "22:00"  # "HH:MM", inclusive
    end: str = "09:00"    # "HH:MM", exclusive; earlier than start: the window runs past midnight

    def active(self, now: time) -> bool:
        """True when a message sent at local time `now` goes out silent."""
        if not self.enabled:
            return False
        start, end = time.fromisoformat(self.start), time.fromisoformat(self.end)
        if start < end:
            return start <= now < end
        return now >= start or now < end

    @classmethod
    def from_dict(cls, data: object) -> QuietHours:
        """The window `{"enabled": bool, "start": "HH:MM", "end": "HH:MM"}` describes; ValueError when it is not one."""
        if not isinstance(data, dict) or set(data) != {"enabled", "start", "end"}:
            raise ValueError('quiet hours must be {"enabled": true|false, "start": "HH:MM", "end": "HH:MM"}')
        if not isinstance(data["enabled"], bool):
            raise ValueError("enabled must be true or false")
        for key in ("start", "end"):
            if not isinstance(data[key], str) or not _HH_MM.fullmatch(data[key]):
                raise ValueError(f"{key} must be a time as HH:MM (got {data[key]!r})")
        if data["start"] == data["end"]:
            raise ValueError("start and end must differ")
        return cls(enabled=data["enabled"], start=data["start"], end=data["end"])

    def to_dict(self) -> dict:
        return asdict(self)

    def update(self, other: QuietHours) -> None:
        """Take `other`'s window in place, so whoever holds this object sees it at once."""
        self.enabled, self.start, self.end = other.enabled, other.start, other.end
