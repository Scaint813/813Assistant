from __future__ import annotations

from datetime import timedelta

from bot.database.queries import get_active_reminders, get_active_tasks, get_overdue_tasks, get_upcoming_overrides


class NextStepService:
    MONEY_WORDS = {"оплат", "деньг", "заказ", "клиент"}

    async def build_next_step(self, user_id: int, session, now, overload: dict | None = None) -> dict:
        await session.flush()
        problem_block_service = None
        try:
            from bot.services.problem_block_service import ProblemBlockService
            problem_block_service = ProblemBlockService()
        except Exception:
            problem_block_service = None
        if overload and overload.get("is_overload"):
            return {
                "mode": "overload",
                "title": "ПЕРЕГРУЗ",
                "situation": "Ресурс просел.",
                "actions": ["Вода.", "Еда.", "20–40 минут отдыха без телефона."],
                "risk_note": "Сначала стабилизация.",
                "related_entities": [],
            }

        tasks = await get_active_tasks(session, user_id)
        overdue = await get_overdue_tasks(session, user_id, now)
        reminders = await get_active_reminders(session, user_id)
        overrides = await get_upcoming_overrides(session, user_id, now.date())
        rest_day = any(str(o.date) == str(now.date()) and o.mode == "rest_day" for o in overrides)

        if not tasks and not reminders:
            return {
                "mode": "empty",
                "title": "Следующий шаг",
                "situation": "Активных действий нет.",
                "actions": ["Зафиксировать новую задачу или открыть Штаб."],
                "risk_note": "",
                "related_entities": [],
            }

        ranked = []
        for t in tasks:
            score = 0
            if t.priority in {"urgent", "high"}:
                score += 50
            elif t.priority == "medium":
                score += 20
            if t.deadline and t.deadline < now:
                score += 30
            lowtxt = f"{t.title} {t.description}".lower()
            if any(w in lowtxt for w in self.MONEY_WORDS):
                score += 20
            if t.is_minor:
                score -= 10
            ranked.append((score, t))
        ranked.sort(key=lambda x: x[0], reverse=True)

        actions = []
        related = []

        if rest_day:
            critical = next((t for _, t in ranked if t.priority in {"urgent", "high"}), None)
            if critical:
                actions.append(f"Закрыть срочное: {critical.title}.")
                related.append({"type": "task", "id": critical.id})
            else:
                actions.append("Закрыть только срочное.")
            actions.append("Остальное не трогать.")
            return {
                "mode": "rest_day",
                "title": "Сегодня режим отдыха.",
                "situation": "Действуем в лёгком режиме.",
                "actions": actions[:3],
                "risk_note": "Без лишних задач.",
                "related_entities": related,
            }

        urgent_overdue = next((t for t in overdue if t.priority in {"urgent", "high"}), None)
        if urgent_overdue:
            actions.append(f"Закрыть просроченное: {urgent_overdue.title}.")
            related.append({"type": "task", "id": urgent_overdue.id})

        near = [r for r in reminders if now <= r.remind_at <= now + timedelta(hours=2)]
        if near:
            actions.append(f"Подготовить напоминание: {near[0].text}.")
            related.append({"type": "reminder", "id": near[0].id})
        if problem_block_service:
            due_blocks = await problem_block_service.get_due_problem_blocks(user_id, session, now)
            if due_blocks:
                b = due_blocks[0]
                actions.append(f"Закрыть блок: {b.title}.")
                actions.append(b.next_action)

        for _, t in ranked:
            if len(actions) >= 3:
                break
            if any(e.get("type") == "task" and e.get("id") == t.id for e in related):
                continue
            actions.append(f"Сделать: {t.title}.")
            related.append({"type": "task", "id": t.id})

        if not actions:
            actions = ["Закрыть один обязательный пункт."]

        return {
            "mode": "urgent" if urgent_overdue else "normal",
            "title": "Следующий шаг",
            "situation": "Фокус на ближайшем действии.",
            "actions": actions[:3],
            "risk_note": "Ограничение: не распыляться.",
            "related_entities": related,
        }
