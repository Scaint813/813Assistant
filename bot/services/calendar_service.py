from __future__ import annotations

from datetime import date, datetime, time, timedelta
from math import ceil
from types import SimpleNamespace

from sqlalchemy import delete, select

from bot.database.models import CalendarEvent, Task, TrainingProfile, UserProfile, WorkoutSession
from bot.services.content_quality import is_meaningful_task
from bot.services.datetime_utils import ensure_aware
from bot.services.preferences_service import PreferencesService


class CalendarService:
    """Stores a read-only calendar mirror and builds plans around real busy time."""

    def __init__(self, time_service, workday_start: time = time(9), workday_end: time = time(21)):
        self.time_service = time_service
        self.workday_start = workday_start
        self.workday_end = workday_end
        self.max_regular_block_minutes = 240
        self.large_task_block_minutes = 90
        self.large_task_daily_minutes = 180

    async def replace_events(self, session, user_id: int, payload: dict) -> int:
        raw_events = payload.get("events") or []
        source = str(payload.get("source") or "shortcut")[:32]
        parsed: list[dict] = []
        for index, raw in enumerate(raw_events[:500]):
            try:
                start_at = self._parse_datetime(raw.get("start_at") or raw.get("start"))
                end_at = self._parse_datetime(raw.get("end_at") or raw.get("end"))
            except (TypeError, ValueError):
                continue
            if end_at <= start_at:
                continue
            parsed.append({
                "external_id": str(raw.get("id") or f"{source}:{start_at.isoformat()}:{index}")[:255],
                "calendar_name": str(raw.get("calendar") or "Основной")[:128],
                "title": str(raw.get("title") or "Занято")[:255],
                "start_at": start_at,
                "end_at": end_at,
                "is_busy": bool(raw.get("is_busy", True)),
            })

        if parsed:
            start = min(row["start_at"] for row in parsed) - timedelta(days=1)
            end = max(row["end_at"] for row in parsed) + timedelta(days=1)
            await session.execute(
                delete(CalendarEvent).where(
                    CalendarEvent.user_id == user_id,
                    CalendarEvent.source == source,
                    CalendarEvent.start_at >= start,
                    CalendarEvent.start_at <= end,
                )
            )
        now = self.time_service.now()
        for row in parsed:
            session.add(CalendarEvent(user_id=user_id, source=source, synced_at=now, **row))
        await session.flush()
        return len(parsed)

    async def events_between(self, session, user_id: int, start: datetime, end: datetime) -> list[CalendarEvent]:
        result = await session.execute(
            select(CalendarEvent).where(
                CalendarEvent.user_id == user_id,
                CalendarEvent.is_busy.is_(True),
                CalendarEvent.start_at < end,
                CalendarEvent.end_at > start,
            ).order_by(CalendarEvent.start_at.asc())
        )
        return list(result.scalars().all())

    async def build_day_plan(self, session, user_id: int, now: datetime, day: date | None = None) -> dict:
        day = day or now.date()
        tz = now.tzinfo
        planning = await self._planning_preferences(session, user_id)
        window_start = datetime.combine(
            day, time(planning["workday_start_hour"]), tzinfo=tz
        )
        window_end = datetime.combine(
            day, time(planning["workday_end_hour"]), tzinfo=tz
        )
        plan_start = max(now, window_start) if day == now.date() else window_start
        events = await self.events_between(session, user_id, window_start, window_end)
        workouts = await self.workouts_between(
            session, user_id, window_start, window_end, tz
        )
        free = self._free_intervals(plan_start, window_end, events + workouts, tz)
        calendar_free_minutes = self._interval_minutes(free)
        capacity_minutes = min(
            calendar_free_minutes, planning["daily_focus_minutes"]
        )
        focus_remaining = capacity_minutes

        result = await session.execute(
            select(Task).where(Task.user_id == user_id, Task.status == "active")
        )
        all_tasks = [task for task in result.scalars().all() if is_meaningful_task(task)]
        inbox = [task for task in all_tasks if task.planning_state == "inbox"]
        ready = [
            task for task in all_tasks
            if task.planning_state == "ready" and task.workflow_state != "blocked"
        ]
        ready.sort(key=lambda task: (
            ensure_aware(task.deadline, tz) if task.deadline else window_end + timedelta(days=3650),
            task.id,
        ))

        for task in all_tasks:
            if task.scheduled_start and ensure_aware(task.scheduled_start, tz).date() == day:
                task.scheduled_start = None
                task.scheduled_end = None

        timeboxes = []
        unscheduled = []
        oversized = []
        for task in ready:
            if focus_remaining < 30:
                unscheduled.append(task)
                continue
            total_minutes = max(5, int(task.estimated_minutes or 30))
            block_limit = planning["large_task_block_minutes"]
            is_large = total_minutes > block_limit
            duration = (
                min(block_limit, total_minutes, focus_remaining)
                if is_large
                else total_minutes
            )
            if not is_large and duration > focus_remaining:
                unscheduled.append(task)
                continue
            slot = (
                self._take_flexible_slot(free, duration, minimum_minutes=30)
                if is_large
                else self._take_slot(free, duration)
            )
            if slot is None:
                unscheduled.append(task)
                continue
            start_at, end_at = slot
            duration = int((end_at - start_at).total_seconds() // 60)
            focus_remaining = max(0, focus_remaining - duration)
            task.scheduled_start = start_at
            task.scheduled_end = end_at
            remaining_minutes = max(0, total_minutes - duration)
            box = {
                "task": task,
                "start": start_at,
                "end": end_at,
                "planned_minutes": duration,
                "total_minutes": total_minutes,
                "remaining_minutes": remaining_minutes,
                "is_partial": is_large,
            }
            timeboxes.append(box)
            if is_large:
                daily_minutes = min(
                    planning["daily_focus_minutes"],
                    max(60, capacity_minutes or planning["daily_focus_minutes"]),
                )
                oversized.append({
                    **box,
                    "daily_minutes": daily_minutes,
                    "estimated_days": ceil(remaining_minutes / daily_minutes),
                })

        await session.flush()
        calendar_remaining_minutes = self._interval_minutes(free)
        remaining_minutes = focus_remaining
        return {
            "day": day,
            "events": events,
            "workouts": workouts,
            "timeboxes": timeboxes,
            "unscheduled": unscheduled,
            "inbox": inbox,
            "capacity_minutes": capacity_minutes,
            "remaining_minutes": remaining_minutes,
            "calendar_free_minutes": calendar_free_minutes,
            "calendar_remaining_minutes": calendar_remaining_minutes,
            "planning": planning,
            # Compatibility for older callers; this now means remaining free time.
            "free_minutes": remaining_minutes,
            "oversized": oversized,
            "connected": bool(events),
        }

    async def workouts_between(
        self, session, user_id: int, start: datetime, end: datetime, tz,
    ) -> list:
        """Return planned workouts as busy intervals for capacity planning."""
        profile_result = await session.execute(
            select(TrainingProfile.session_minutes).where(
                TrainingProfile.user_id == user_id
            )
        )
        session_minutes = int(profile_result.scalar_one_or_none() or 45)
        result = await session.execute(
            select(WorkoutSession).where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.status.in_(("planned", "in_progress")),
                WorkoutSession.scheduled_for < end,
                WorkoutSession.scheduled_for >= start - timedelta(minutes=session_minutes),
            ).order_by(WorkoutSession.scheduled_for.asc())
        )
        intervals = []
        for workout in result.scalars().all():
            workout_start = ensure_aware(workout.scheduled_for, tz)
            workout_end = workout_start + timedelta(minutes=session_minutes)
            if workout_start < end and workout_end > start:
                intervals.append(SimpleNamespace(
                    start_at=workout_start,
                    end_at=workout_end,
                    title=workout.title,
                    workout_id=workout.id,
                ))
        return intervals

    async def _planning_preferences(self, session, user_id: int) -> dict[str, int]:
        result = await session.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            return {
                "workday_start_hour": self.workday_start.hour,
                "workday_end_hour": self.workday_end.hour,
                "daily_focus_minutes": self.large_task_daily_minutes,
                "large_task_block_minutes": self.large_task_block_minutes,
                "planning_weekends": True,
            }
        return PreferencesService().planning(profile)

    async def find_free_start(
        self, session, user_id: int, preferred: datetime, duration_minutes: int
    ) -> datetime:
        day_start = preferred.replace(hour=8, minute=0, second=0, microsecond=0)
        day_end = preferred.replace(hour=22, minute=0, second=0, microsecond=0)
        events = await self.events_between(session, user_id, day_start, day_end)
        free = self._free_intervals(max(preferred, day_start), day_end, events, preferred.tzinfo)
        slot = self._take_slot(free, duration_minutes)
        return slot[0] if slot else preferred

    def _parse_datetime(self, value) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return ensure_aware(parsed, self.time_service.now().tzinfo)

    @staticmethod
    def _free_intervals(start: datetime, end: datetime, events: list[CalendarEvent], tz) -> list[list[datetime]]:
        if start >= end:
            return []
        free: list[list[datetime]] = []
        cursor = start
        for event in events:
            event_start = max(start, ensure_aware(event.start_at, tz))
            event_end = min(end, ensure_aware(event.end_at, tz))
            if event_end <= cursor:
                continue
            if event_start > cursor:
                free.append([cursor, event_start])
            cursor = max(cursor, event_end)
        if cursor < end:
            free.append([cursor, end])
        return free

    @staticmethod
    def _take_slot(free: list[list[datetime]], minutes: int) -> tuple[datetime, datetime] | None:
        required = timedelta(minutes=minutes)
        for interval in free:
            if interval[1] - interval[0] >= required:
                start = interval[0]
                end = start + required
                interval[0] = end
                return start, end
        return None

    @staticmethod
    def _take_flexible_slot(
        free: list[list[datetime]], preferred_minutes: int, minimum_minutes: int,
    ) -> tuple[datetime, datetime] | None:
        preferred = timedelta(minutes=preferred_minutes)
        minimum = timedelta(minutes=minimum_minutes)
        for interval in free:
            available = interval[1] - interval[0]
            if available < minimum:
                continue
            start = interval[0]
            end = start + min(preferred, available)
            interval[0] = end
            return start, end
        return None

    @staticmethod
    def _interval_minutes(intervals: list[list[datetime]]) -> int:
        return sum(
            max(0, int((end - start).total_seconds() // 60))
            for start, end in intervals
        )
