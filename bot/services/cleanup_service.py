from __future__ import annotations

from bot.database.queries import archive_task, find_cleanup_candidates

PROTECTED_KEYWORDS = {"оплатить", "деньги", "заказ", "клиент", "егэ", "экзамен", "дедлайн", "долг", "доставка", "выкуп", "сдать", "проверить оплату", "важно", "срочно"}


class CleanupService:
    async def run(self, session, user_id: int, now):
        candidates = await find_cleanup_candidates(session, user_id, now)
        archived = []
        for task in candidates:
            text = f"{task.title} {task.description}".lower()
            if any(k in text for k in PROTECTED_KEYWORDS) or task.priority == "high":
                continue
            await archive_task(session, task, "Автоархив: мелкая просроченная задача 3+ дня")
            archived.append(task)
        return archived
