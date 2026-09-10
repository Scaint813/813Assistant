from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin, urlparse

import httpx
from icalendar import Calendar
from sqlalchemy import func, select

from bot.database.models import CalendarEvent, CalendarSyncState
from bot.services.datetime_utils import ensure_aware

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HSESyncResult:
    imported: int
    first_start: datetime | None
    last_end: datetime | None
    source: str = "hse_ical"


@dataclass(frozen=True, slots=True)
class HSESnapshotAssessment:
    status: str
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status == "complete"


class HSECalendarService:
    """Parse HSE iCalendar exports into the shared calendar event model."""

    SOURCE = "hse_ical"
    MAX_ICAL_BYTES = 5 * 1024 * 1024
    DEFAULT_WINDOW_DAYS = 14
    DISTANT_SINGLE_EVENT_DAYS = 7

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
        on_sync=None,
    ):
        self.calendar_service = calendar_service
        self.session_factory = session_factory
        self.time_service = time_service
        self.user_id = user_id
        self.feed_url = feed_url.strip()
        self.enabled = enabled
        self.sync_interval_minutes = max(15, int(sync_interval_minutes))
        self.on_sync = on_sync

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
        target_user_id = user_id or self.user_id
        async with self.session_factory() as session:
            imported = await self.calendar_service.replace_events(
                session, target_user_id, payload
            )
            await session.commit()
        if self.on_sync is not None:
            try:
                await self.on_sync(target_user_id, "calendar_import")
            except Exception:
                logger.exception(
                    "Automatic plan rebuild after HSE import failed: user=%s",
                    target_user_id,
                )
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
                CalendarEvent.source.in_((self.SOURCE, "hse_ios")),
                CalendarEvent.end_at >= now,
            )
        )
        count, synced_at, first_start, last_end = result.one()
        ical_count = await session.scalar(
            select(func.count(CalendarEvent.id)).where(
                CalendarEvent.user_id == (user_id or self.user_id),
                CalendarEvent.source == self.SOURCE,
                CalendarEvent.end_at >= now,
            )
        )
        sync_state = await session.scalar(
            select(CalendarSyncState).where(
                CalendarSyncState.user_id == (user_id or self.user_id),
                CalendarSyncState.source == "hse_ios",
            )
        )
        synced_at = ensure_aware(synced_at, now.tzinfo)
        state_success = None
        if sync_state:
            state_success = sync_state.last_complete_at
            if state_success is None and sync_state.last_result != "incomplete":
                state_success = sync_state.last_success_at
        state_synced_at = ensure_aware(state_success, now.tzinfo)
        if state_synced_at and (not synced_at or state_synced_at > synced_at):
            synced_at = state_synced_at
        last_attempt_at = ensure_aware(
            sync_state.last_attempt_at if sync_state else None,
            now.tzinfo,
        )
        if last_attempt_at and (not synced_at or last_attempt_at > synced_at):
            synced_at = last_attempt_at
        integrity_status = (
            sync_state.integrity_status if sync_state else "unknown"
        )
        integrity_reason = (
            sync_state.integrity_reason if sync_state else ""
        )
        if int(ical_count or 0) > 0:
            integrity_status = "complete"
            integrity_reason = ""
        elif integrity_status in {"", "unknown"}:
            assessment = self.assess_snapshot(
                event_count=int(count or 0),
                first_start=ensure_aware(first_start, now.tzinfo),
                now=now,
                existing_count=0,
            )
            integrity_status = assessment.status
            integrity_reason = assessment.reason
        has_complete_snapshot = bool(
            (sync_state and sync_state.last_complete_at)
            or (
                sync_state
                and integrity_status == "complete"
                and sync_state.last_result != "incomplete"
            )
            or (sync_state is None and integrity_status == "complete")
            or int(ical_count or 0) > 0
        )
        usable_count = int(count or 0) if has_complete_snapshot else 0
        return {
            "configured": self.is_configured,
            "events": usable_count,
            "stored_events": int(count or 0),
            "synced_at": synced_at,
            "first_start": ensure_aware(first_start, now.tzinfo),
            "last_end": ensure_aware(last_end, now.tzinfo),
            "last_result": sync_state.last_result if sync_state else None,
            "received_count": (
                sync_state.received_count if sync_state else int(count or 0)
            ),
            "window_days": (
                sync_state.window_days
                if sync_state else self.DEFAULT_WINDOW_DAYS
            ),
            "integrity_status": integrity_status,
            "integrity_reason": integrity_reason,
            "integrity_message": self.integrity_message(
                integrity_status,
                integrity_reason,
                received_count=(
                    sync_state.received_count if sync_state else int(count or 0)
                ),
            ),
            "last_attempt_at": last_attempt_at,
            "last_complete_at": ensure_aware(
                sync_state.last_complete_at if sync_state else None,
                now.tzinfo,
            ),
            "consecutive_failures": (
                sync_state.consecutive_failures if sync_state else 0
            ),
            "has_complete_snapshot": has_complete_snapshot,
        }

    @classmethod
    def assess_snapshot(
        cls,
        *,
        event_count: int,
        first_start: datetime | None,
        now: datetime,
        existing_count: int = 0,
        explicit_complete: bool = False,
    ) -> HSESnapshotAssessment:
        """Classify a device snapshot before it can replace known-good data."""
        if explicit_complete:
            return HSESnapshotAssessment("complete")
        if event_count == 0:
            if existing_count > 0:
                return HSESnapshotAssessment(
                    "incomplete", "empty_while_future_events_exist"
                )
            return HSESnapshotAssessment("complete")
        if (
            event_count == 1
            and first_start is not None
            and first_start > now + timedelta(days=cls.DISTANT_SINGLE_EVENT_DAYS)
        ):
            return HSESnapshotAssessment(
                "incomplete", "only_one_distant_event"
            )
        if existing_count >= 4 and event_count * 2 < existing_count:
            return HSESnapshotAssessment("incomplete", "snapshot_shrank")
        return HSESnapshotAssessment("complete")

    @staticmethod
    def integrity_message(
        status: str,
        reason: str,
        *,
        received_count: int,
    ) -> str:
        if status != "incomplete":
            return ""
        if reason == "only_one_distant_event":
            return (
                f"Получено только {received_count} событие, а ближайшее находится "
                "дальше чем через неделю. Расписание может быть неполным."
            )
        if reason == "empty_while_future_events_exist":
            return (
                "Телефон прислал пустой снимок, хотя в рабочем календаре ещё есть "
                "будущие занятия. Последние известные данные сохранены."
            )
        if reason == "snapshot_shrank":
            return (
                "Новый снимок заметно меньше предыдущего. Последнее полноценное "
                "расписание сохранено до следующей проверки."
            )
        return "Источник прислал неполный снимок расписания."

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
        leading_room_match = re.match(
            r"^([А-ЯA-Z]?\d{2,4}[А-ЯA-Z]?)\s*,\s*(.+)$",
            location,
            flags=re.IGNORECASE,
        )
        if leading_room_match:
            return leading_room_match.group(2).strip(), leading_room_match.group(1)
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
