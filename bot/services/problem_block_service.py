from __future__ import annotations

import json
from datetime import datetime, timedelta

from bot.database.queries import add_problem_event, create_problem_block, get_active_problem_blocks, get_problem_block_by_id


class ProblemBlockService:
    VALID_CATEGORIES = {"study", "exam", "money", "orders", "health", "training", "sleep", "conflict", "work", "discipline", "overload", "system", "other"}

    def _norm_priority(self, value: str | None) -> str:
        return value if value in {"low", "medium", "high", "urgent"} else "medium"

    def _norm_pressure(self, value: str | None) -> str:
        return value if value in {"soft", "normal", "hard"} else "normal"

    async def create_problem_block(self, user_id: int, intent: dict, session, now: datetime, source_type: str = "text", resources: list[dict] | None = None):
        category = intent.get("category") if intent.get("category") in self.VALID_CATEGORIES else "other"
        deadline = datetime.fromisoformat(intent["deadline"]) if intent.get("deadline") else None
        if category == "exam" and not deadline and now.date().isoformat() <= "2026-06-18":
            deadline = datetime.fromisoformat("2026-06-18T23:59:00+00:00")
        review_delta = timedelta(hours=6 if self._norm_priority(intent.get("priority")) in {"high", "urgent"} else 24)
        block = await create_problem_block(
            session,
            user_id,
            category=category,
            title=intent.get("title") or "Активный блок",
            description=intent.get("description") or "",
            problem_text=intent.get("problem_text") or "",
            solution_strategy=intent.get("solution_strategy") or "",
            next_action=intent.get("next_action") or "Сделать первый короткий шаг.",
            status="active",
            priority=self._norm_priority(intent.get("priority")),
            pressure_level=self._norm_pressure(intent.get("pressure_level")),
            deadline=deadline,
            next_review_at=now + review_delta,
            source_type=source_type,
            resources_json=json.dumps(resources or [], ensure_ascii=False),
        )
        await add_problem_event(session, user_id, block.id, "created", block.title)
        return block

    async def get_active_problem_blocks(self, user_id: int, session, now: datetime):
        return await get_active_problem_blocks(session, user_id, now)

    async def get_due_problem_blocks(self, user_id: int, session, now: datetime):
        items = await get_active_problem_blocks(session, user_id, now)
        return [i for i in items if i.next_review_at and i.next_review_at <= now]

    async def pick_checkin_problem_block(self, user_id: int, session, checkin_type: str, now: datetime):
        items = await get_active_problem_blocks(session, user_id, now)
        due = [i for i in items if (not i.next_review_at) or i.next_review_at <= now]
        if not due:
            return None
        pr = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
        due.sort(
            key=lambda b: (
                pr.get(b.priority, 9),
                b.deadline or datetime.max.replace(tzinfo=now.tzinfo),
                b.created_at,
            )
        )
        return due[0]

    async def snooze_problem_block(self, user_id: int, block_id: int, until: datetime, session, comment: str):
        item = await get_problem_block_by_id(session, user_id, block_id)
        if not item:
            return None
        item.status = "active"
        item.next_review_at = until
        await add_problem_event(session, user_id, item.id, "snoozed", comment)
        return item

    async def archive_expired_problem_blocks(self, user_id: int, session, now: datetime):
        items = await get_active_problem_blocks(session, user_id, now + timedelta(days=3650))
        expired = []
        for item in items:
            if item.deadline and item.deadline < now:
                item.status = "expired"
                item.archived_at = now
                item.archive_reason = "deadline passed"
                await add_problem_event(session, user_id, item.id, "expired", "deadline passed")
                expired.append(item)
        return expired

    async def complete_problem_block(self, user_id: int, block_id: int, session, now: datetime):
        item = await get_problem_block_by_id(session, user_id, block_id)
        if not item:
            return None
        item.status = "done"
        item.archived_at = now
        await add_problem_event(session, user_id, item.id, "done")
        return item

    async def archive_problem_block(self, user_id: int, block_id: int, reason: str, session, now: datetime):
        item = await get_problem_block_by_id(session, user_id, block_id)
        if not item:
            return None
        item.status = "archived"
        item.archived_at = now
        item.archive_reason = reason
        await add_problem_event(session, user_id, item.id, "archived", reason)
        return item

    def build_problem_solution_summary(self, block) -> str:
        steps = [s.strip() for s in (block.solution_strategy or "").split(".") if s.strip()][:3]
        if not steps:
            steps = ["Снизить риск ошибки.", "Зафиксировать единый подход.", "Проверить результат."]
        body = "\n".join(f"{i+1}. {step}." for i, step in enumerate(steps[:3]))
        return f"ПРОБЛЕМА\n{block.problem_text or block.title}\n\nРЕШЕНИЕ\n{body}\n\nСЛЕДУЮЩИЙ ШАГ\n{block.next_action}"
