from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin, urlparse

import httpx
from icalendar import Calendar
from sqlalchemy import func, select

from bot.database.models import CalendarEvent
from bot.services.datetime_utils import ensure_aware

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HSESyncResult:
    imported: int
    first_start: datetime | None
    last_end: datetime | None
    source: str = "hse_ical"


class HSECalendarService:
    """Parse HSE iCalendar exports into the shared calendar event model."""

    SOURCE = "hse_ical"
    MAX_ICAL_BYTES = 5 * 1024 * 1024

    def __init__(
        self,
        calendar_service,
        session_factory,
        time_service,
        user_id: int,
        *,
        feed_url: str = "",
        enabled: bool = False,
        sync_interval_minutes: int = 60,
    ):
        self.calendar_service = calendar_service
        self.session_factory = session_factory
        self.time_service = time_service
        self.user_id = user_id
        self.feed_url = feed_url.strip()
        self.enabled = enabled
        self.sync_interval_minutes = max(15, int(sync_interval_minutes))

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.feed_url)

    def schedule(self, scheduler) -> bool:
        if not self.is_configured:
            return False
        scheduler.add_job(
            self._scheduled_sync,
            trigger="interval",
            minutes=self.sync_interval_minutes,
            id="system:hse_ical_sync",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=self.time_service.now() + timedelta(seconds=5),
        )
        return True

    async def _scheduled_sync(self) -> None:
        try:
            result = await self.sync_configured_feed()
            logger.info("HSE iCal sync completed: events=%s", result.imported)
        except Exception as exc:
            # The URL may contain a private token, so it must never appear in logs.
            logger.error("HSE iCal sync failed: %s", type(exc).__name__)

    async def sync_configured_feed(self) -> HSESyncResult:
        if not self.is_configured:
            raise RuntimeError("HSE iCal sync is not configured")
        content = await self._download(self.feed_url)
        return await self.import_bytes(content)

    async def _download(self, raw_url: str) -> bytes:
        url = self._validated_url(raw_url)
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            for _ in range(4):
                response = await client.get(
                    url, headers={"Accept": "text/calendar"}
                )
                if not response.is_redirect:
                    break
                location = response.headers.get("location", "")
                url = self._validated_url(urljoin(url, location))
            else:
                raise ValueError("Too many iCal redirects")
            response.raise_for_status()
        self._validated_url(str(response.url))
        content = response.content
        if len(content) > self.MAX_ICAL_BYTES:
            raise ValueError("iCal file is larger than 5 MB")
        return content

    @staticmethod
    def _validated_url(raw_url: str) -> str:
        normalized = raw_url.strip()
        if normalized.startswith("webcal://"):
            normalized = "https://" + normalized[len("webcal://"):]
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not (host == "hse.ru" or host.endswith(".hse.ru")):
            raise ValueError("Only HTTPS iCal links on hse.ru are allowed")
        return normalized

    async def import_bytes(
        self, content: bytes | str, *, user_id: int | None = None
    ) -> HSESyncResult:
        payload = self.parse_ical(content)
        async with self.session_factory() as session:
            imported = await self.calendar_service.replace_events(
                session, user_id or self.user_id, payload
            )
            await session.commit()
        starts = [datetime.fromisoformat(item["start"]) for item in payload["events"]]
        ends = [datetime.fromisoformat(item["end"]) for item in payload["events"]]
        return HSESyncResult(
            imported=imported,
            first_start=min(starts, default=None),
            last_end=max(ends, default=None),
        )

    def parse_ical(self, content: bytes | str) -> dict:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        if len(raw) > self.MAX_ICAL_BYTES:
            raise ValueError("iCal file is larger than 5 MB")
        try:
            calendar = Calendar.from_ical(raw)
        except Exception as exc:
            raise ValueError("Could not parse iCal file") from exc

        parsed_by_id: dict[str, dict] = {}
        for index, component in enumerate(calendar.walk("VEVENT")):
            if str(component.get("STATUS", "")).casefold() == "cancelled":
                continue
            try:
                start_value = component.decoded("DTSTART")
            except (KeyError, ValueError, TypeError):
                continue
            start_at = self._event_datetime(start_value)
            end_at = self._event_end(component, start_value, start_at)
            if end_at <= start_at:
                continue

            uid = self._clean_text(component.get("UID")) or f"event-{index}"
            recurrence = component.get("RECURRENCE-ID")
            if recurrence is not None:
                try:
                    recurrence_value = self._event_datetime(recurrence.dt)
                    uid = f"{uid}:{recurrence_value.isoformat()}"
                except (AttributeError, TypeError, ValueError):
                    uid = f"{uid}:recurrence-{index}"

            title = self._clean_text(component.get("SUMMARY")) or "Занятие ВШЭ"
            description = self._clean_text(component.get("DESCRIPTION"))
            location = self._clean_text(component.get("LOCATION"))
            teacher = self._extract_teacher(description)
            building, room = self._split_location(location)
            external_id = f"{self.SOURCE}:{uid}"
            parsed_by_id[external_id] = {
                "id": external_id[:255],
                "calendar": "ВШЭ",
                "title": title[:255],
                "event_type": self._event_type(title, description),
                "description": description[:4000],
                "location": location[:255],
                "teacher": teacher[:255],
                "building": building[:255],
                "room": room[:64],
                "start": start_at.isoformat(),
                "end": end_at.isoformat(),
                "is_busy": True,
            }

        events = sorted(parsed_by_id.values(), key=lambda item: (item["start"], item["id"]))
        if not events:
            raise ValueError("No calendar events found in iCal file")
        return {"source": self.SOURCE, "replace_all": True, "events": events}

    def _event_datetime(self, value: date | datetime) -> datetime:
        if isinstance(value, datetime):
            return ensure_aware(value, self.time_service.tz)
        if isinstance(value, date):
            return datetime.combine(value, time.min, tzinfo=self.time_service.tz)
        raise TypeError("Unsupported iCal date")

    def _event_end(self, component, start_value, start_at: datetime) -> datetime:
        try:
            end_value = component.decoded("DTEND")
        except (KeyError, ValueError, TypeError):
            end_value = None
        if end_value is not None:
            return self._event_datetime(end_value)
        try:
            duration = component.decoded("DURATION")
        except (KeyError, ValueError, TypeError):
            duration = None
        if isinstance(duration, timedelta) and duration.total_seconds() > 0:
            return start_at + duration
        return start_at + (timedelta(days=1) if type(start_value) is date else timedelta(hours=1))

    async def status(
        self, session, now: datetime, *, user_id: int | None = None
    ) -> dict:
        result = await session.execute(
            select(
                func.count(CalendarEvent.id),
                func.max(CalendarEvent.synced_at),
                func.min(CalendarEvent.start_at),
                func.max(CalendarEvent.end_at),
            ).where(
                CalendarEvent.user_id == (user_id or self.user_id),
                CalendarEvent.source == self.SOURCE,
                CalendarEvent.end_at >= now,
            )
        )
        count, synced_at, first_start, last_end = result.one()
        return {
            "configured": self.is_configured,
            "events": int(count or 0),
            "synced_at": ensure_aware(synced_at, now.tzinfo),
            "first_start": ensure_aware(first_start, now.tzinfo),
            "last_end": ensure_aware(last_end, now.tzinfo),
        }

    @staticmethod
    def _clean_text(value) -> str:
        return "\n".join(
            line.strip() for line in str(value or "").replace("\\n", "\n").splitlines()
            if line.strip()
        )

    @staticmethod
    def _event_type(title: str, description: str) -> str:
        text = f"{title} {description}".casefold()
        if any(word in text for word in ("лекция", "lecture")):
            return "lecture"
        if any(word in text for word in ("семинар", "seminar")):
            return "seminar"
        if any(word in text for word in ("экзамен", "exam", "зачёт", "зачет")):
            return "exam"
        if any(word in text for word in ("практичес", "practice")):
            return "practice"
        return "class"

    @staticmethod
    def _extract_teacher(description: str) -> str:
        match = re.search(
            r"(?:преподаватель|лектор|teacher|lecturer)\s*[:—-]\s*([^\n;]+)",
            description,
            flags=re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _split_location(location: str) -> tuple[str, str]:
        if not location:
            return "", ""
        room_match = re.search(
            r"(?:ауд(?:итория)?\.?|room)\s*[:№#-]?\s*([\w.-]+)",
            location,
            flags=re.IGNORECASE,
        )
        room = room_match.group(1).strip() if room_match else ""
        building = location
        if room_match:
            building = location[:room_match.start()].rstrip(" ,;—-")
        elif match := re.search(r"[,;]\s*([А-ЯA-Z]?\d{2,4}[А-ЯA-Z]?)\s*$", location):
            room = match.group(1)
            building = location[:match.start()].rstrip(" ,;")
        return building or location, room
