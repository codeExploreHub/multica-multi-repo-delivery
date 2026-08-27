"""Injectable UTC clock boundary."""

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current timezone-aware instant."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
