from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from bot.database.models import FocusSession, Task
from bot.services.content_quality import is_meaningful_task


class FocusService:
    async def pick_task(self, session, user_id: int, now: datetime) -> Task | None:
        result = await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status == "active",
                Task.planning_state == "ready",
                Task.workflow_state != "blocked",
            ).order_by(Task.scheduled_start.asc().nullslast(), Task.deadline.asc().nullslast(), Task.id.asc())
        )
        return next((task for task in result.scalars().all() if is_meaningful_task(task)), None)

    async def start(self, session, user_id: int, task_id: int, now: datetime, minutes: int) -> FocusSession | None:
        task = await session.get(Task, task_id)
        if not task or task.user_id != user_id or task.status != "active":
            return None
        active_result = await session.execute(
            select(FocusSession).where(FocusSession.user_id == user_id, FocusSession.status == "active")
        )
        for active in active_result.scalars().all():
            active.status = "abandoned"
            active.ended_at = now
        row = FocusSession(
            user_id=user_id, task_id=task.id, status="active",
            planned_minutes=max(5, min(180, minutes)), started_at=now,
        )
        session.add(row)
        task.workflow_state = "now"
        await session.flush()
        return row

    async def active(self, session, user_id: int) -> FocusSession | None:
        result = await session.execute(
            select(FocusSession).where(
                FocusSession.user_id == user_id, FocusSession.status == "active"
            ).order_by(FocusSession.started_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def finish(self, session, user_id: int, now: datetime, action: str) -> tuple[FocusSession | None, Task | None]:
        focus = await self.active(session, user_id)
        if not focus:
            return None, None
        task = await session.get(Task, focus.task_id)
        if action == "extend":
            focus.planned_minutes += 15
            await session.flush()
            return focus, task
        focus.ended_at = now
        focus.status = action
        if task and action == "completed":
            task.status = "done"
            task.workflow_state = "done"
            task.completed_at = now
        elif task and action == "blocked":
            task.workflow_state = "blocked"
            task.blocked_reason = "Нужно уточнить препятствие после фокус-сессии"
        await session.flush()
        return focus, task
