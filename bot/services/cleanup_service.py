from __future__ import annotations

from bot.database.queries import archive_task, find_cleanup_candidates, has_active_reminder_for_task

PROTECTED_KEYWORDS = {"оплатить", "деньги", "заказ", "клиент", "егэ", "экзамен", "дедлайн", "долг", "доставка", "выкуп", "сдать", "проверить оплату", "важно", "срочно"}


class CleanupService:
    async def run(self, session, user_id: int, now):
        candidates = await find_cleanup_candidates(session, user_id, now)
        archived = []
        for task in candidates:
            if task.priority not in {None, "", "low"}:
                continue
            text = f"{task.title} {task.description}".lower()
            if any(k in text for k in PROTECTED_KEYWORDS):
                continue
            if await has_active_reminder_for_task(session, user_id, task.id):
                continue
            await archive_task(session, task, "minor overdue 3+ days", now)
            archived.append(task)
        return archived
