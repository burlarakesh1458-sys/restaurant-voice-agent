"""Injectable clock. The scenario suite pins time so "tonight" always means the same date."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

RESTAURANT_TZ = ZoneInfo("Asia/Kolkata")


class Clock:
    def now(self) -> datetime:
        return datetime.now(RESTAURANT_TZ)


class FixedClock(Clock):
    def __init__(self, iso: str):
        dt = datetime.fromisoformat(iso)
        self._dt = dt if dt.tzinfo else dt.replace(tzinfo=RESTAURANT_TZ)

    def now(self) -> datetime:
        return self._dt
