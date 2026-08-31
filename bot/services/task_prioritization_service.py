from __future__ import annotations

from datetime import timedelta

from bot.services.content_quality import is_meaningful_task
from bot.services.datetime_utils import ensure_aware


class TaskPrioritizationService:
    """Order tasks by observable facts, never by model-invented importance scores."""

    def score(self, task, now) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []

        if task.deadline:
            deadline = ensure_aware(task.deadline, now.tzinfo)
            delta = deadline - now
            if delta.total_seconds() < 0:
                score += 300
                reasons.append("срок уже прошёл")
            elif delta <= timedelta(hours=24):
                score += 200
                reasons.append("срок наступает в течение суток")
            elif delta <= timedelta(days=3):
                score += 100
                reasons.append("срок в ближайшие три дня")

        if task.blocked_reason:
            score -= 1000
            reasons.append(f"заблокировано: {task.blocked_reason[:80]}")
        if not reasons:
            reasons.append("порядок по времени добавления")
        return score, reasons

    async def refresh(self, session, user_id: int, now) -> list:
        from bot.database.queries import get_active_tasks

        tasks = await get_active_tasks(session, user_id)
        ranked: list[tuple[int, object, list[str]]] = []
        for task in tasks:
            score, reasons = self.score(task, now)
            if not is_meaningful_task(task):
                score = -1000
                reasons = ["Нужно уточнить, что именно сделать."]
            ranked.append((score, task, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1].id))

        now_count = 0
        for _score, task, reasons in ranked:
            if task.planning_state == "inbox":
                task.workflow_state = "inbox"
                task.priority_reason = "Нужно уточнить срок и длительность."
                continue
            if not is_meaningful_task(task) or task.blocked_reason:
                task.workflow_state = "blocked"
            elif now_count < 3:
                task.workflow_state = "now"
                now_count += 1
            else:
                task.workflow_state = "next"
            task.priority_reason = "; ".join(reasons)
        await session.flush()
        return [task for _, task, _ in ranked]
