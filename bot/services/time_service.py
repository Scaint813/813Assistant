from __future__ import annotations

import re
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
        if "через" in normalized and "дн" in normalized:
            if "два" in normalized or "2" in normalized:
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
        if m := re.search(r"\bв\s*(\d{1,2})(?::(\d{2}))?\b", normalized):
            hh = int(m.group(1))
            mm = int(m.group(2) or 0)
            return time(hour=hh, minute=mm)
        if m := re.search(r"\bк\s*(\d{1,2})(?::(\d{2}))?\s*веч", normalized):
            hh = int(m.group(1))
            if hh < 12:
                hh += 12
            mm = int(m.group(2) or 0)
            return time(hour=hh, minute=mm)
        if ":" in normalized:
            hh, mm = normalized.split(":", 1)
            return time(hour=int(hh), minute=int(mm))
        return self._default_map["day"]

    def build_datetime(self, date_label: str | None, time_label: str | None) -> datetime:
        d = self.parse_relative_date(date_label)
        t = self.parse_relative_time(time_label)
        return datetime.combine(d, t, tzinfo=self.tz)

    def parse_datetime_any(self, raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self.tz)
            return dt
        except ValueError:
            return None
