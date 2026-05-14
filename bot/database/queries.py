from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import CleanupLog, PendingPreview, Reminder, ScheduleOverride, Task


async def get_active_tasks(session: AsyncSession, user_id: int) -> list[Task]:
    res = await session.execute(select(Task).where(and_(Task.user_id == user_id, Task.status == "active")))
    return list(res.scalars().all())


async def get_active_reminders(session: AsyncSession, user_id: int) -> list[Reminder]:
    res = await session.execute(select(Reminder).where(and_(Reminder.user_id == user_id, Reminder.status == "active")).order_by(Reminder.remind_at.asc()).limit(5))
    return list(res.scalars().all())


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


async def create_schedule_override(session: AsyncSession, user_id: int, date, mode: str, **kwargs) -> ScheduleOverride:
    item = ScheduleOverride(user_id=user_id, date=date, mode=mode, **kwargs)
    session.add(item)
    await session.flush()
    return item


async def save_pending_preview(session: AsyncSession, user_id: int, source_text: str, intents: dict) -> PendingPreview:
    preview = PendingPreview(user_id=user_id, source_text=source_text, intents_json=json.dumps(intents, ensure_ascii=False))
    session.add(preview)
    await session.flush()
    return preview


async def get_pending_preview(session: AsyncSession, user_id: int) -> PendingPreview | None:
    res = await session.execute(select(PendingPreview).where(and_(PendingPreview.user_id == user_id, PendingPreview.status == "pending")).order_by(PendingPreview.created_at.desc()).limit(1))
    return res.scalars().first()


async def resolve_pending_preview(session: AsyncSession, preview: PendingPreview, status: str) -> None:
    preview.status = status
    await session.flush()


async def find_cleanup_candidates(session: AsyncSession, user_id: int, now: datetime) -> list[Task]:
    threshold = now - timedelta(days=3)
    res = await session.execute(
        select(Task).where(
            and_(
                Task.user_id == user_id,
                Task.status == "active",
                Task.deadline.is_not(None),
                Task.deadline < threshold,
                Task.is_minor.is_(True),
                Task.auto_cleanup_allowed.is_(True),
            )
        )
    )
    return list(res.scalars().all())


async def archive_task(session: AsyncSession, task: Task, reason: str) -> None:
    task.status = "archived"
    task.archived_at = datetime.utcnow()
    task.cleanup_reason = reason
    session.add(CleanupLog(user_id=task.user_id, entity_type="task", entity_id=task.id, action="archive", reason=reason))
    await session.flush()
