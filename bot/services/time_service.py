from __future__ import annotations

from datetime import datetime, timedelta


class TimeService:
    def __init__(self, tz, defaults: dict[str, str]):
        self.tz = tz
        self.defaults = defaults

    def now(self) -> datetime:
        return datetime.now(tz=self.tz)

    def resolve_day(self, label: str):
        today = self.now().date()
        normalized = label.lower().strip()
        if normalized in {"today", "сегодня"}:
            return today
        if normalized in {"tomorrow", "завтра"}:
            return today + timedelta(days=1)
        if normalized in {"послезавтра"}:
            return today + timedelta(days=2)
        return today
