from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select

from bot.database.models import ActionLog, ProblemBlock, Reminder, ScheduleOverride, Task


class ActionLogService:
    """User-visible audit trail with recoverable create operations."""

    async def log_create(
        self,
        session,
        user_id: int,
        entity_type: str,
        entity_id: int,
        summary: str,
        batch_key: str,
        source: str,
        after: dict | None = None,
    ) -> ActionLog:
        row = ActionLog(
            user_id=user_id,
            batch_key=batch_key,
            action_type="create",
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            source=source,
            after_json=json.dumps(after or {}, ensure_ascii=False, default=str),
            status="applied",
            undoable=True,
        )
        session.add(row)
        await session.flush()
        return row

    async def log_update(
        self,
        session,
        user_id: int,
        entity_type: str,
        entity_id: int,
        summary: str,
        before: dict,
        after: dict,
        batch_key: str,
        source: str,
        undoable: bool = False,
    ) -> ActionLog:
        row = ActionLog(
            user_id=user_id,
            batch_key=batch_key,
            action_type="update",
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            before_json=json.dumps(before, ensure_ascii=False, default=str),
            after_json=json.dumps(after, ensure_ascii=False, default=str),
            source=source,
            status="applied",
            undoable=undoable,
        )
        session.add(row)
        await session.flush()
        return row

    async def recent(self, session, user_id: int, limit: int = 20) -> list[ActionLog]:
        result = await session.execute(
            select(ActionLog)
            .where(ActionLog.user_id == user_id)
            .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def latest_undoable_batch(self, session, user_id: int) -> str | None:
        result = await session.execute(
            select(ActionLog.batch_key)
            .where(
                ActionLog.user_id == user_id,
                ActionLog.status == "applied",
                ActionLog.undoable.is_(True),
            )
            .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def undo_batch(
        self,
        session,
        user_id: int,
        batch_key: str,
        now: datetime,
    ) -> dict:
        result = await session.execute(
            select(ActionLog)
            .where(
                ActionLog.user_id == user_id,
                ActionLog.batch_key == batch_key,
                ActionLog.status == "applied",
                ActionLog.undoable.is_(True),
            )
            .order_by(ActionLog.id.desc())
        )
        logs = list(result.scalars().all())
        if not logs:
            return {"undone": 0, "reminder_ids": [], "reschedule_reminder_ids": [], "summaries": []}

        reminder_ids: list[int] = []
        reschedule_reminder_ids: list[int] = []
        summaries: list[str] = []
        undone = 0
        for log in logs:
            entity = None
            if log.action_type == "update" and log.entity_type in {"task", "reminder", "problem_block"}:
                model = {
                    "task": Task,
                    "reminder": Reminder,
                    "problem_block": ProblemBlock,
                }[log.entity_type]
                entity = await session.get(model, log.entity_id)
                if entity and entity.user_id == user_id:
                    before = json.loads(log.before_json or "{}")
                    for key, value in before.items():
                        if not hasattr(entity, key):
                            continue
                        current = getattr(entity, key)
                        if isinstance(current, datetime) and isinstance(value, str):
                            try:
                                value = datetime.fromisoformat(value)
                            except ValueError:
                                pass
                        setattr(entity, key, value)
                    if log.entity_type == "reminder":
                        if entity.status == "active":
                            reschedule_reminder_ids.append(entity.id)
                        else:
                            reminder_ids.append(entity.id)
            elif log.entity_type == "task":
                entity = await session.get(Task, log.entity_id)
                if entity and entity.user_id == user_id:
                    await session.delete(entity)
            elif log.entity_type == "reminder":
                entity = await session.get(Reminder, log.entity_id)
                if entity and entity.user_id == user_id:
                    entity.status = "cancelled"
                    reminder_ids.append(entity.id)
            elif log.entity_type == "problem_block":
                entity = await session.get(ProblemBlock, log.entity_id)
                if entity and entity.user_id == user_id:
                    entity.status = "archived"
                    entity.archived_at = now
                    entity.archive_reason = "undo"
            elif log.entity_type == "schedule_override":
                entity = await session.get(ScheduleOverride, log.entity_id)
                if entity and entity.user_id == user_id:
                    await session.delete(entity)

            if entity is None:
                log.status = "undo_failed"
                log.undoable = False
                continue
            log.status = "undone"
            log.undone_at = now
            log.undoable = False
            summaries.append(log.summary)
            undone += 1

        await session.flush()
        return {
            "undone": undone,
            "reminder_ids": reminder_ids,
            "reschedule_reminder_ids": reschedule_reminder_ids,
            "summaries": summaries,
        }
