from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import CleanupLog, MiroMapping, PendingPreview, ProblemBlock, ProblemBlockEvent, Reminder, ScheduleOverride, Task, UserProfile, UserRuntimeState


async def get_or_create_user_profile(session: AsyncSession, user_id: int, name: str, timezone: str) -> UserProfile:
    res = await session.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    profile = res.scalar_one_or_none()
    if profile:
        return profile
    profile = UserProfile(user_id=user_id, name=name, timezone=timezone)
    session.add(profile)
    await session.flush()
    return profile


async def get_or_create_runtime_state(session: AsyncSession, user_id: int) -> UserRuntimeState:
    res = await session.execute(select(UserRuntimeState).where(UserRuntimeState.user_id == user_id))
    state = res.scalar_one_or_none()
    if state:
        return state
    state = UserRuntimeState(user_id=user_id)
    session.add(state)
    await session.flush()
    return state


async def touch_user_activity(session: AsyncSession, user_id: int, now: datetime) -> None:
    state = await get_or_create_runtime_state(session, user_id)
    state.last_user_activity_at = now
    await session.flush()


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


async def get_reminder_by_id(session: AsyncSession, user_id: int, reminder_id: int) -> Reminder | None:
    res = await session.execute(select(Reminder).where(and_(Reminder.id == reminder_id, Reminder.user_id == user_id)))
    return res.scalar_one_or_none()


async def create_schedule_override(session: AsyncSession, user_id: int, override_date: date, mode: str, **kwargs) -> ScheduleOverride:
    item = ScheduleOverride(user_id=user_id, date=override_date, mode=mode, **kwargs)
    session.add(item)
    await session.flush()
    return item


async def find_task_by_title(session: AsyncSession, user_id: int, title: str) -> Task | None:
    res = await session.execute(select(Task).where(and_(Task.user_id == user_id, Task.title == title, Task.status == "active")).order_by(Task.id.desc()))
    return res.scalar_one_or_none()


async def get_active_tasks(session: AsyncSession, user_id: int) -> list[Task]:
    res = await session.execute(select(Task).where(and_(Task.user_id == user_id, Task.status == "active")).order_by(Task.created_at.desc()))
    return list(res.scalars().all())


async def get_tasks_for_date(session: AsyncSession, user_id: int, day_start: datetime, day_end: datetime) -> list[Task]:
    res = await session.execute(select(Task).where(and_(Task.user_id == user_id, Task.status == "active", Task.deadline >= day_start, Task.deadline < day_end)))
    return list(res.scalars().all())


async def get_overdue_tasks(session: AsyncSession, user_id: int, now: datetime) -> list[Task]:
    res = await session.execute(select(Task).where(and_(Task.user_id == user_id, Task.status == "active", Task.deadline.is_not(None), Task.deadline < now)).order_by(Task.deadline.asc()))
    return list(res.scalars().all())


async def get_active_reminders(session: AsyncSession, user_id: int) -> list[Reminder]:
    res = await session.execute(select(Reminder).where(and_(Reminder.user_id == user_id, Reminder.status == "active")).order_by(Reminder.remind_at.asc()))
    return list(res.scalars().all())


async def get_all_active_reminders(session: AsyncSession) -> list[Reminder]:
    res = await session.execute(select(Reminder).where(Reminder.status == "active").order_by(Reminder.remind_at.asc()))
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
    res = await session.execute(
        select(Reminder.id).where(
            and_(
                Reminder.user_id == user_id,
                Reminder.status == "active",
                Reminder.related_entity_type == "task",
                Reminder.related_entity_id == task_id,
            )
        )
    )
    return res.first() is not None


async def archive_task(session: AsyncSession, task: Task, reason: str, now: datetime) -> None:
    task.status = "archived"
    task.archived_at = now
    task.cleanup_reason = reason
    session.add(CleanupLog(user_id=task.user_id, entity_type="task", entity_id=task.id, action="archive", reason=reason))
    await session.flush()


async def get_archived_tasks(session: AsyncSession, user_id: int) -> list[Task]:
    res = await session.execute(select(Task).where(and_(Task.user_id == user_id, Task.status == "archived")).order_by(Task.archived_at.desc()))
    return list(res.scalars().all())


async def create_problem_block(session: AsyncSession, user_id: int, **kwargs) -> ProblemBlock:
    item = ProblemBlock(user_id=user_id, **kwargs)
    session.add(item)
    await session.flush()
    return item


async def add_problem_event(session: AsyncSession, user_id: int, problem_block_id: int, event_type: str, comment: str = "") -> ProblemBlockEvent:
    event = ProblemBlockEvent(user_id=user_id, problem_block_id=problem_block_id, event_type=event_type, comment=comment)
    session.add(event)
    await session.flush()
    return event


async def get_problem_block_by_id(session: AsyncSession, user_id: int, block_id: int) -> ProblemBlock | None:
    res = await session.execute(select(ProblemBlock).where(and_(ProblemBlock.user_id == user_id, ProblemBlock.id == block_id)))
    return res.scalar_one_or_none()


async def get_active_problem_blocks(session: AsyncSession, user_id: int, now: datetime) -> list[ProblemBlock]:
    res = await session.execute(
        select(ProblemBlock).where(
            and_(
                ProblemBlock.user_id == user_id,
                ProblemBlock.status == "active",
                (ProblemBlock.deadline.is_(None) | (ProblemBlock.deadline >= now)),
            )
        ).order_by(ProblemBlock.priority.desc(), ProblemBlock.created_at.desc())
    )
    return list(res.scalars().all())


async def get_miro_mapping(session: AsyncSession, user_id: int, entity_type: str, entity_id: int, board_id: str) -> MiroMapping | None:
    res = await session.execute(
        select(MiroMapping).where(
            and_(
                MiroMapping.user_id == user_id,
                MiroMapping.entity_type == entity_type,
                MiroMapping.entity_id == entity_id,
                MiroMapping.board_id == board_id,
            )
        )
    )
    return res.scalar_one_or_none()


async def get_or_create_miro_mapping(session: AsyncSession, user_id: int, entity_type: str, entity_id: int, board_id: str) -> MiroMapping:
    row = await get_miro_mapping(session, user_id, entity_type, entity_id, board_id)
    if row:
        return row
    row = MiroMapping(user_id=user_id, entity_type=entity_type, entity_id=entity_id, board_id=board_id)
    session.add(row)
    await session.flush()
    return row


async def get_user_profile(session: AsyncSession, user_id: int) -> UserProfile | None:
    res = await session.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    return res.scalar_one_or_none()


async def get_archived_problem_blocks(session: AsyncSession, user_id: int) -> list[ProblemBlock]:
    res = await session.execute(
        select(ProblemBlock).where(
            and_(ProblemBlock.user_id == user_id, ProblemBlock.status.in_(["archived", "expired", "done"]))
        ).order_by(ProblemBlock.archived_at.desc())
    )
    return list(res.scalars().all())


async def get_done_reminders_count(session: AsyncSession, user_id: int) -> int:
    from sqlalchemy import func
    res = await session.execute(
        select(func.count()).where(
            and_(Reminder.user_id == user_id, Reminder.status.in_(["done", "cancelled"]))
        )
    )
    return res.scalar_one() or 0


async def get_tasks_by_keywords(session: AsyncSession, user_id: int, keywords: list[str]) -> list[Task]:
    """Return active tasks whose title contains any of the keywords (case-insensitive)."""
    from sqlalchemy import or_
    filters = [Task.title.ilike(f"%{kw}%") for kw in keywords]
    res = await session.execute(
        select(Task).where(and_(Task.user_id == user_id, Task.status == "active", or_(*filters)))
        .order_by(Task.created_at.desc())
    )
    return list(res.scalars().all())


async def get_reminders_by_keywords(session: AsyncSession, user_id: int, keywords: list[str]) -> list[Reminder]:
    """Return active reminders whose text contains any of the keywords (case-insensitive)."""
    from sqlalchemy import or_
    filters = [Reminder.text.ilike(f"%{kw}%") for kw in keywords]
    res = await session.execute(
        select(Reminder).where(and_(Reminder.user_id == user_id, Reminder.status == "active", or_(*filters)))
        .order_by(Reminder.remind_at.asc())
    )
    return list(res.scalars().all())


async def get_problem_blocks_by_categories(session: AsyncSession, user_id: int, categories: list[str], now: datetime) -> list[ProblemBlock]:
    """Return active problem blocks matching given categories."""
    from sqlalchemy import or_
    filters = [ProblemBlock.category == cat for cat in categories]
    res = await session.execute(
        select(ProblemBlock).where(
            and_(
                ProblemBlock.user_id == user_id,
                ProblemBlock.status == "active",
                (ProblemBlock.deadline.is_(None) | (ProblemBlock.deadline >= now)),
                or_(*filters),
            )
        ).order_by(ProblemBlock.created_at.desc())
    )
    return list(res.scalars().all())

