from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo


class TimeService:
    WEEKDAYS: ClassVar[dict[str, int]] = {
        "понедельник": 0, "понедельникам": 0,
        "вторник": 1, "вторникам": 1,
        "среда": 2, "средам": 2, "среду": 2,
        "четверг": 3, "четвергам": 3,
        "пятница": 4, "пятницам": 4, "пятницу": 4,
        "суббота": 5, "субботам": 5, "субботу": 5,
        "воскресенье": 6, "воскресеньям": 6,
    }
    def __init__(self, tz: ZoneInfo, morning: time, day: time, evening: time, night: time):
        self.tz = tz
        self._default_map = {"morning": morning, "day": day, "evening": evening, "night": night}

    def now(self) -> datetime:
        return datetime.now(tz=self.tz)

    def in_timezone(self, timezone: str | ZoneInfo) -> TimeService:
        """Create an isolated clock for one user without mutating the shared service."""
        tz = timezone if isinstance(timezone, ZoneInfo) else ZoneInfo(str(timezone))
        scoped = TimeService(
            tz,
            self._default_map["morning"],
            self._default_map["day"],
            self._default_map["evening"],
            self._default_map["night"],
        )
        # Keep the source clock as the single notion of "now".  Apart from
        # making timezone conversion deterministic, this preserves frozen
        # clocks used by tests and by maintenance commands.
        scoped.now = lambda: self.now().astimezone(tz)
        return scoped

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
        if "через" in normalized and "дн" in normalized and ("два" in normalized or "2" in normalized):
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
        if m := re.search(r"\bв\s*(\d{1,2})(?:[.:](\d{2}))?\b", normalized):
            hh = int(m.group(1))
            mm = int(m.group(2) or 0)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return time(hour=hh, minute=mm)
        if m := re.search(r"\bк\s*(\d{1,2})(?:[.:](\d{2}))?\s*веч", normalized):
            hh = int(m.group(1))
            if hh < 12:
                hh += 12
            mm = int(m.group(2) or 0)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return time(hour=hh, minute=mm)
        if m := re.fullmatch(r"\s*(\d{1,2})[.:](\d{2})\s*", normalized):
            hh, mm = int(m.group(1)), int(m.group(2))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return time(hour=hh, minute=mm)
        return self._default_map["day"]

    def extract_time_from_text(self, text: str | None) -> time | None:
        """Extract an explicit clock time, including common Russian ``18.20`` notation."""
        normalized = (text or "").lower()
        match = re.search(r"(?<!\d)([01]?\d|2[0-3])[.:]([0-5]\d)(?!\d)", normalized)
        if match:
            return time(hour=int(match.group(1)), minute=int(match.group(2)))
        match = re.search(r"\b(?:в|к)\s*([01]?\d|2[0-3])(?:\s*(?:час(?:а|ов)?|ч))?\b", normalized)
        if match:
            return time(hour=int(match.group(1)), minute=0)
        return None

    def extract_date_from_text(self, text: str | None) -> date | None:
        """Extract an explicit relative date; return ``None`` when the date was omitted."""
        normalized = (text or "").lower()
        if "послезавтра" in normalized or "через два дня" in normalized or "через 2 дня" in normalized:
            return self.today() + timedelta(days=2)
        if "завтра" in normalized:
            return self.tomorrow()
        if "сегодня" in normalized:
            return self.today()
        for word, weekday in self.WEEKDAYS.items():
            if word in normalized:
                delta = (weekday - self.today().weekday()) % 7
                return self.today() + timedelta(days=delta)
        return None

    def parse_recurrence(self, text: str | None, candidate: str | None = None) -> str:
        normalized = (text or "").lower()
        value = (candidate or "none").lower().strip()
        if value in {"daily", "weekdays", "weekly", "monthly"}:
            return value
        if any(marker in normalized for marker in ("каждый день", "каждое утро", "каждый вечер", "ежедневно")):
            return "daily"
        if any(marker in normalized for marker in ("по будням", "каждый будний")):
            return "weekdays"
        if any(marker in normalized for marker in ("каждый месяц", "ежемесячно")):
            return "monthly"
        if "кажд" in normalized and any(word in normalized for word in self.WEEKDAYS):
            return "weekly"
        return "none"

    def next_recurrence(self, current: datetime, recurrence: str, after: datetime | None = None) -> datetime | None:
        base = current if current.tzinfo else current.replace(tzinfo=self.tz)
        after = after or self.now()
        if recurrence == "none":
            return None
        candidate = base
        if recurrence == "daily":
            while candidate <= after:
                candidate += timedelta(days=1)
        elif recurrence == "weekdays":
            while candidate <= after:
                candidate += timedelta(days=1)
                while candidate.weekday() >= 5:
                    candidate += timedelta(days=1)
        elif recurrence == "weekly":
            while candidate <= after:
                candidate += timedelta(days=7)
        elif recurrence == "monthly":
            while candidate <= after:
                year = candidate.year + (1 if candidate.month == 12 else 0)
                month = 1 if candidate.month == 12 else candidate.month + 1
                day = min(candidate.day, monthrange(year, month)[1])
                candidate = candidate.replace(year=year, month=month, day=day)
        else:
            return None
        return candidate

    def parse_snooze_text(self, text: str) -> datetime | None:
        normalized = text.lower().strip()
        now = self.now()
        match = re.search(r"через\s+(\d+)\s*(мин|час|ч\b|дн)", normalized)
        if match:
            amount = max(1, int(match.group(1)))
            unit = match.group(2)
            if unit.startswith("мин"):
                return now + timedelta(minutes=amount)
            if unit.startswith("дн"):
                return now + timedelta(days=amount)
            return now + timedelta(hours=amount)
        explicit_time = self.extract_time_from_text(normalized)
        explicit_date = self.extract_date_from_text(normalized)
        if explicit_time:
            result = datetime.combine(explicit_date or now.date(), explicit_time, tzinfo=self.tz)
            if explicit_date is None and result <= now:
                result += timedelta(days=1)
            return result
        if "завтра" in normalized:
            return self.build_datetime("tomorrow", "morning")
        if "вечером" in normalized:
            candidate = self.build_datetime("today", "evening")
            return candidate if candidate > now else self.build_datetime("tomorrow", "evening")
        return None

    def build_reminder_datetime(
        self,
        text: str,
        *,
        date_label: str | None = None,
        time_label: str | None = None,
        parsed: datetime | None = None,
    ) -> datetime:
        """Resolve reminder time while giving explicit user text the highest priority.

        A time without a date means the next occurrence of that clock time. This avoids
        silently saving an already-expired reminder when a user writes ``18.20`` late
        in the evening. The Action Preview always shows the resolved date and time.
        """
        now = self.now()
        explicit_date = self.extract_date_from_text(text)
        explicit_time = self.extract_time_from_text(text)

        if parsed is not None:
            base = parsed if parsed.tzinfo else parsed.replace(tzinfo=self.tz)
        else:
            base = self.build_datetime(date_label, time_label)

        target_date = explicit_date or base.date()
        target_time = explicit_time or base.timetz().replace(tzinfo=None)
        result = datetime.combine(target_date, target_time, tzinfo=self.tz)

        if explicit_time and explicit_date is None and not date_label and result <= now:
            result += timedelta(days=1)
        return result

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
