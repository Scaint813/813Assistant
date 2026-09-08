from __future__ import annotations

from datetime import date, datetime, time, timedelta
from html import escape

from sqlalchemy import select

from bot.database.models import Reminder, Task
from bot.services.datetime_utils import ensure_aware


class DailyBriefService:
    """Build one grounded screen from calendar, tasks, reminders and conflicts."""

    MONTHS = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )

    def __init__(self, calendar_service, conflict_service):
        self.calendar_service = calendar_service
        self.conflict_service = conflict_service

    async def build(
        self,
        session,
        user_id: int,
        now: datetime,
        day: date | None = None,
    ) -> str:
        day = day or now.date()
        start = datetime.combine(day, time.min, tzinfo=now.tzinfo)
        end = start + timedelta(days=1)
        plan = await self.calendar_service.build_day_plan(
            session,
            user_id,
            now,
            day=day,
            persist_schedule=False,
        )
        events = await self.calendar_service.events_between(
            session, user_id, start, end
        )
        context = await self.conflict_service.route_estimator.user_context(
            session, user_id
        )
        conflicts = self.conflict_service.detect(events, now.tzinfo, context)

        reminder_result = await session.execute(
            select(Reminder).where(
                Reminder.user_id == user_id,
                Reminder.status == "active",
                Reminder.remind_at >= start,
                Reminder.remind_at < end,
            ).order_by(Reminder.remind_at.asc())
        )
        reminders = list(reminder_result.scalars().all())

        deadline_end = end + timedelta(days=3)
        deadline_result = await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status == "active",
                Task.deadline.is_not(None),
                Task.deadline < deadline_end,
            ).order_by(Task.deadline.asc())
        )
        deadlines = list(deadline_result.scalars().all())

        lines = [f"Сводка дня · {day.day} {self.MONTHS[day.month - 1]}"]
        hse_events = [item for item in events if item.source == "hse_ical"]
        other_events = [item for item in events if item.source != "hse_ical"]

        lines += ["", "Пары:"]
        if hse_events:
            for event in hse_events[:8]:
                event_start = ensure_aware(event.start_at, now.tzinfo)
                event_end = ensure_aware(event.end_at, now.tzinfo)
                place = f" · {escape(event.location)}" if event.location else ""
                lines.append(
                    f"• {event_start.strftime('%H:%M')}–{event_end.strftime('%H:%M')} · "
                    f"{escape(event.title)}{place}"
                )
        else:
            lines.append("• расписание ВШЭ ещё не загружено")

        if other_events:
            lines += ["", "Другие события:"]
            for event in other_events[:5]:
                event_start = ensure_aware(event.start_at, now.tzinfo)
                event_end = ensure_aware(event.end_at, now.tzinfo)
                lines.append(
                    f"• {event_start.strftime('%H:%M')}–{event_end.strftime('%H:%M')} · "
                    f"{escape(event.title)}"
                )

        departure = self._departure_line(events, context, now)
        if departure:
            lines += ["", departure]

        lines += ["", "План по свободным окнам:"]
        if plan["timeboxes"]:
            for box in plan["timeboxes"][:6]:
                lines.append(
                    f"• {box['start'].strftime('%H:%M')}–{box['end'].strftime('%H:%M')} · "
                    f"{escape(box['task'].title)}"
                )
        else:
            lines.append("• готовых задач для раскладки нет")
        if plan["unscheduled"]:
            lines.append(f"Не поместилось сегодня: {len(plan['unscheduled'])}.")
        if plan["inbox"]:
            lines.append(f"Без срока или длительности: {len(plan['inbox'])}.")

        lines += ["", "Ближайшие дедлайны:"]
        if deadlines:
            for task in deadlines[:6]:
                deadline = ensure_aware(task.deadline, now.tzinfo)
                if deadline < now:
                    when = "просрочено"
                elif deadline.date() == day:
                    when = f"сегодня {deadline.strftime('%H:%M')}"
                elif deadline.date() == day + timedelta(days=1):
                    when = f"завтра {deadline.strftime('%H:%M')}"
                else:
                    when = deadline.strftime("%d.%m %H:%M")
                lines.append(f"• {when} · {escape(task.title)}")
        else:
            lines.append("• на ближайшие три дня нет")

        if reminders:
            lines += ["", "Напоминания:"]
            for reminder in reminders[:5]:
                remind_at = ensure_aware(reminder.remind_at, now.tzinfo)
                lines.append(
                    f"• {remind_at.strftime('%H:%M')} · {escape(reminder.text)}"
                )

        lines += ["", "Конфликты:"]
        if conflicts:
            for conflict in conflicts[:4]:
                if conflict.kind == "overlap":
                    lines.append(
                        f"• пересечение {conflict.missing_minutes} мин: "
                        f"{escape(conflict.first.title)} / {escape(conflict.second.title)}"
                    )
                else:
                    mark = "≈" if conflict.approximate else ""
                    lines.append(
                        f"• на переход не хватает {conflict.missing_minutes} мин "
                        f"(нужно {mark}{conflict.required_minutes})"
                    )
        else:
            lines.append("• не найдено")

        return "\n".join(lines)

    async def build_review(
        self,
        session,
        user_id: int,
        now: datetime,
        day: date | None = None,
    ) -> str:
        """Summarize the day from persisted facts and preview tomorrow's risks."""
        day = day or now.date()
        start = datetime.combine(day, time.min, tzinfo=now.tzinfo)
        end = start + timedelta(days=1)
        completed_result = await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status == "completed",
                Task.completed_at >= start,
                Task.completed_at < end,
            ).order_by(Task.completed_at.asc())
        )
        completed = list(completed_result.scalars().all())
        overdue_result = await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status == "active",
                Task.deadline.is_not(None),
                Task.deadline < now,
            ).order_by(Task.deadline.asc()).limit(6)
        )
        overdue = list(overdue_result.scalars().all())
        events = await self.calendar_service.events_between(
            session, user_id, start, end
        )
        tomorrow_start = end
        tomorrow_conflicts = await self.conflict_service.detect_between(
            session, user_id, tomorrow_start, tomorrow_start + timedelta(days=1)
        )

        lines = [f"Итоги дня · {day.day} {self.MONTHS[day.month - 1]}", ""]
        lines.append(f"Событий в календаре: {len(events)}.")
        lines.append(f"Завершено задач: {len(completed)}.")
        if completed:
            lines.extend(f"• {escape(task.title)}" for task in completed[:6])
        lines += ["", "Осталось просроченным:"]
        if overdue:
            lines.extend(f"• {escape(task.title)}" for task in overdue)
        else:
            lines.append("• ничего")
        lines += ["", "Завтра:"]
        if tomorrow_conflicts:
            lines.append(f"• конфликтов в расписании: {len(tomorrow_conflicts)}")
        else:
            lines.append("• конфликтов в расписании не найдено")
        return "\n".join(lines)

    def _departure_line(self, events, context: dict, now: datetime) -> str:
        home = self.conflict_service.route_estimator.home(context)
        candidates = [
            event for event in events
            if event.location and ensure_aware(event.start_at, now.tzinfo) > now
        ]
        if not home or not candidates:
            return ""
        first = min(candidates, key=lambda item: ensure_aware(item.start_at, now.tzinfo))
        estimate = self.conflict_service.route_estimator.estimate(
            home, first.location, context
        )
        leave_at = ensure_aware(first.start_at, now.tzinfo) - timedelta(
            minutes=estimate.minutes
        )
        mark = "≈" if estimate.approximate else ""
        return (
            f"Выйти из дома: не позднее {leave_at.strftime('%H:%M')} "
            f"({mark}{estimate.minutes} мин до {escape(first.location)} с запасом)."
        )
