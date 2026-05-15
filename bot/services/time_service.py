from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


class TimeService:
    def __init__(self, tz: ZoneInfo, morning: time, day: time, evening: time, night: time):
        self.tz = tz
        self._default_map = {"morning": morning, "day": day, "evening": evening, "night": night}

    def now(self) -> datetime:
        return datetime.now(tz=self.tz)

    def today(self) -> date:
        return self.now().date()

    def tomorrow(self) -> date:
        return self.today() + timedelta(days=1)

    def parse_relative_date(self, label: str | None) -> date:
        normalized = (label or "today").lower().strip()
        if normalized in {"tomorrow", "завтра"}:
            return self.tomorrow()
        if normalized in {"послезавтра"}:
            return self.today() + timedelta(days=2)
        return self.today()

    def parse_relative_time(self, label: str | None) -> time:
        normalized = (label or "day").lower().strip()
        if normalized in {"утром", "morning"}:
            return self._default_map["morning"]
        if normalized in {"днем", "днём", "day"}:
            return self._default_map["day"]
        if normalized in {"вечером", "evening"}:
            return self._default_map["evening"]
        if normalized in {"ночью", "night"}:
            return self._default_map["night"]
        if ":" in normalized:
            hh, mm = normalized.split(":", 1)
            return time(hour=int(hh), minute=int(mm))
        return self._default_map["day"]

    def build_datetime(self, date_label: str | None, time_label: str | None) -> datetime:
        d = self.parse_relative_date(date_label)
        t = self.parse_relative_time(time_label)
        return datetime.combine(d, t, tzinfo=self.tz)
