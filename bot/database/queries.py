from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import CleanupLog, PendingPreview, Reminder, ScheduleOverride, Task, UserProfile


async def get_or_create_user_profile(session: AsyncSession, user_id: int, name: str, timezone: str) -> UserProfile:
    res = await session.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    profile = res.scalar_one_or_none()
    if profile:
        return profile
    profile = UserProfile(user_id=user_id, name=name, timezone=timezone)
    session.add(profile)
    await session.flush()
    return profile


async def create_pending_preview(session: AsyncSession, user_id: int, source_type: str, original_text: str, transcript: str, preview: dict) -> PendingPreview:
    row = PendingPreview(user_id=user_id, source_type=source_type, original_text=original_text, transcript=transcript, preview_json=json.dumps(preview, ensure_ascii=False))
    session.add(row)
    await session.flush()
    return row


async def get_pending_preview(session: AsyncSession, preview_id: int, user_id: int) -> PendingPreview | None:
    res = await session.execute(select(PendingPreview).where(and_(PendingPreview.id == preview_id, PendingPreview.user_id == user_id)))
    return res.scalar_one_or_none()


async def create_task(session: AsyncSession, user_id: int, title: str, **kwargs) -> Task:
    task = Task(user_id=user_id, title=title, **kwargs)
    session.add(task)
    await session.flush()
    return task


async def create_reminder(session: AsyncSession, user_id: int, text: str, remind_at: datetime, **kwargs) -> Reminder:
    reminder = Reminder(user_id=user_id, text=text, remind_at=remind_at, **kwargs)
    session.add(reminder)
    await session.flush()
    return reminder


async def create_schedule_override(session: AsyncSession, user_id: int, override_date: date, mode: str, **kwargs) -> ScheduleOverride:
    item = ScheduleOverride(user_id=user_id, date=override_date, mode=mode, **kwargs)
    session.add(item)
    await session.flush()
    return item


async def get_active_tasks(session: AsyncSession, user_id: int) -> list[Task]:
    res = await session.execute(select(Task).where(and_(Task.user_id == user_id, Task.status == "active")).order_by(Task.created_at.desc()))
    return list(res.scalars().all())


async def get_tasks_for_date(session: AsyncSession, user_id: int, day_start: datetime, day_end: datetime) -> list[Task]:
    res = await session.execute(select(Task).where(and_(Task.user_id == user_id, Task.status == "active", Task.deadline >= day_start, Task.deadline < day_end)))
    return list(res.scalars().all())


async def get_active_reminders(session: AsyncSession, user_id: int) -> list[Reminder]:
    res = await session.execute(select(Reminder).where(and_(Reminder.user_id == user_id, Reminder.status == "active")).order_by(Reminder.remind_at.asc()))
    return list(res.scalars().all())


async def get_reminders_for_date(session: AsyncSession, user_id: int, day_start: datetime, day_end: datetime) -> list[Reminder]:
    res = await session.execute(select(Reminder).where(and_(Reminder.user_id == user_id, Reminder.status == "active", Reminder.remind_at >= day_start, Reminder.remind_at < day_end)))
    return list(res.scalars().all())


async def get_upcoming_overrides(session: AsyncSession, user_id: int, from_date: date) -> list[ScheduleOverride]:
    res = await session.execute(select(ScheduleOverride).where(and_(ScheduleOverride.user_id == user_id, ScheduleOverride.date >= from_date)).order_by(ScheduleOverride.date.asc()))
    return list(res.scalars().all())


async def find_cleanup_candidates(session: AsyncSession, user_id: int, now: datetime) -> list[Task]:
    threshold = now - timedelta(days=3)
    res = await session.execute(select(Task).where(and_(Task.user_id == user_id, Task.status == "active", Task.deadline.is_not(None), Task.deadline < threshold, Task.is_minor.is_(True), Task.auto_cleanup_allowed.is_(True))))
    return list(res.scalars().all())


async def has_active_reminder_for_task(session: AsyncSession, user_id: int, task_id: int) -> bool:
    # no relation in MVP schema; always False
    return False


async def archive_task(session: AsyncSession, task: Task, reason: str, now: datetime) -> None:
    task.status = "archived"
    task.archived_at = now
    task.cleanup_reason = reason
    session.add(CleanupLog(user_id=task.user_id, entity_type="task", entity_id=task.id, action="archive", reason=reason))
    await session.flush()
