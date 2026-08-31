from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, select

from bot.database.models import Project, Task
from bot.database.queries import (
    get_active_reminders,
    get_active_tasks,
    get_overdue_tasks,
    get_upcoming_overrides,
)
from bot.services.content_quality import is_meaningful_task, task_action
from bot.services.datetime_utils import ensure_aware


class NextStepService:
    async def pick_existing_task(self, user_id: int, session, now) -> dict:
        """Choose one saved task without creating or mutating anything."""
        tasks = [
            task for task in await get_active_tasks(session, user_id)
            if is_meaningful_task(task)
        ]
        candidates = [
            task for task in tasks
            if getattr(task, "planning_state", "ready") == "ready"
            and getattr(task, "workflow_state", "next") != "blocked"
            and not getattr(task, "blocked_reason", "")
        ]
        if not candidates:
            if tasks:
                return {
                    "status": "needs_details",
                    "text": (
                        "Сохранённые задачи есть, но для выбора не хватает срока или длительности. "
                        "Напиши: «в задаче про документы срок завтра, займёт 30 минут»."
                    ),
                }
            return {
                "status": "empty",
                "text": "Сохранённых активных задач пока нет. Я ничего нового не создавал.",
            }

        far_future = now + timedelta(days=3650)
        candidates.sort(key=lambda task: (
            ensure_aware(task.scheduled_start, now.tzinfo)
            if getattr(task, "scheduled_start", None) else far_future,
            ensure_aware(task.deadline, now.tzinfo) if task.deadline else far_future,
            task.id,
        ))
        task = candidates[0]
        action = task_action(task) or task.title
        total_minutes = max(5, int(task.estimated_minutes or 30))
        work_block_minutes = min(total_minutes, 90 if total_minutes > 240 else total_minutes)

        reason = "первая готовая задача по порядку"
        if task.deadline:
            deadline = ensure_aware(task.deadline, now.tzinfo)
            if deadline < now:
                reason = "срок уже прошёл"
            elif deadline.date() == now.date():
                reason = f"срок сегодня в {deadline.strftime('%H:%M')}"
            elif deadline <= now + timedelta(days=3):
                reason = f"ближайший срок — {deadline.strftime('%d.%m %H:%M')}"
        return {
            "status": "selected",
            "task": task,
            "action": action,
            "reason": reason,
            "total_minutes": total_minutes,
            "work_block_minutes": work_block_minutes,
            "related_entity": {"type": "task", "id": task.id},
        }

    async def build_next_step(self, user_id: int, session, now, overload: dict | None = None) -> dict:
        await session.flush()
        problem_block_service = None
        try:
            from bot.services.problem_block_service import ProblemBlockService
            problem_block_service = ProblemBlockService()
        except ImportError:
            problem_block_service = None
        if overload and overload.get("is_overload"):
            return {
                "mode": "overload",
                "title": "Снижаем нагрузку",
                "situation": "Оставляем только обязательные дела.",
                "actions": [
                    "Сделать паузу на 30 минут.",
                    "После паузы открыть план и выбрать одну задачу.",
                ],
                "risk_note": "Новые необязательные дела лучше перенести.",
                "related_entities": [],
            }

        tasks = [task for task in await get_active_tasks(session, user_id) if is_meaningful_task(task)]
        overdue = [task for task in await get_overdue_tasks(session, user_id, now) if is_meaningful_task(task)]
        reminders = await get_active_reminders(session, user_id)
        overrides = await get_upcoming_overrides(session, user_id, now.date())
        rest_day = any(str(o.date) == str(now.date()) and o.mode == "rest_day" for o in overrides)

        if not tasks and not reminders:
            return {
                "mode": "empty",
                "title": "План пока пуст",
                "situation": "Активных действий нет.",
                "actions": ["Написать новую задачу обычным сообщением."],
                "risk_note": "",
                "related_entities": [],
            }

        ranked = [
            task for task in tasks
            if getattr(task, "workflow_state", "next") != "blocked"
            and getattr(task, "planning_state", "ready") == "ready"
        ]
        far_future = now + timedelta(days=3650)
        ranked.sort(key=lambda task: (
            ensure_aware(task.scheduled_start, now.tzinfo)
            if getattr(task, "scheduled_start", None) else far_future,
            ensure_aware(task.deadline, now.tzinfo) if task.deadline else far_future,
            task.id,
        ))

        actions = []
        related = []

        if rest_day:
            end_of_day = now.replace(hour=23, minute=59, second=59)
            critical = next(
                (task for task in ranked if task.deadline and ensure_aware(task.deadline, now.tzinfo) <= end_of_day),
                None,
            )
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

        urgent_overdue = next(
            (task for task in overdue if getattr(task, "planning_state", "ready") == "ready"),
            None,
        )
        if urgent_overdue:
            actions.append(f"Закрыть просроченное: {urgent_overdue.title}.")
            related.append({"type": "task", "id": urgent_overdue.id})

        tz = now.tzinfo
        near = []
        for r in reminders:
            if not r.remind_at:
                continue
            remind_at = ensure_aware(r.remind_at, tz)
            if remind_at and now <= remind_at <= now + timedelta(hours=2):
                near.append(r)
        if near:
            actions.append(f"Подготовить напоминание: {near[0].text}.")
            related.append({"type": "reminder", "id": near[0].id})
        if problem_block_service:
            due_blocks = await problem_block_service.get_due_problem_blocks(user_id, session, now)
            if due_blocks:
                b = due_blocks[0]
                actions.append(f"Разобрать препятствие: {b.title}.")
                if b.next_action:
                    actions.append(b.next_action)

        for t in ranked:
            if len(actions) >= 3:
                break
            if any(e.get("type") == "task" and e.get("id") == t.id for e in related):
                continue
            action = task_action(t)
            if not action:
                continue
            actions.append(f"{action}.")
            related.append({"type": "task", "id": t.id})

        if not actions:
            actions = (
                ["Открыть Входящие и уточнить срок с длительностью."]
                if any(getattr(task, "planning_state", "ready") == "inbox" for task in tasks)
                else ["Уточнить одну конкретную задачу и её первый шаг."]
            )

        return {
            "mode": "urgent" if urgent_overdue else "normal",
            "title": "Следующий шаг",
            "situation": "Фокус на ближайшем действии.",
            "actions": actions[:3],
            "risk_note": "Ограничение: не распыляться.",
            "related_entities": related,
        }

    async def build_proactive_suggestion(self, user_id: int, session, now) -> dict | None:
        """Return a suggestion only when a concrete task has a real near-term deadline."""
        tasks = await get_active_tasks(session, user_id)
        candidates = []
        for task in tasks:
            if not is_meaningful_task(task) or getattr(task, "workflow_state", "next") == "blocked":
                continue
            if not task.deadline:
                continue
            deadline = ensure_aware(task.deadline, now.tzinfo)
            if deadline > now + timedelta(hours=24):
                continue
            action = task_action(task)
            if not action:
                continue
            candidates.append((deadline, task, action))

        capacity = await self._capacity_warning(user_id, session, now)
        if capacity:
            return capacity
        if not candidates:
            return await self._stale_project_warning(user_id, session, now)

        candidates.sort(key=lambda item: item[0])
        deadline, task, action = candidates[0]
        if deadline < now:
            heading = "Срок этой задачи уже прошёл:"
        elif deadline.date() == now.date():
            heading = f"Сегодня до {deadline.strftime('%H:%M')} нужно:"
        else:
            heading = f"Завтра до {deadline.strftime('%H:%M')} нужно:"

        lines = [heading, task.title]
        if action.casefold() != task.title.casefold():
            lines += ["", f"Начать с: {action}"]
        return {
            "text": "\n".join(lines),
            "related_entities": [{"type": "task", "id": task.id}],
        }

    async def _capacity_warning(self, user_id: int, session, now) -> dict | None:
        end_of_day = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now >= end_of_day:
            return None
        result = await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status == "active",
                Task.deadline.is_not(None),
                Task.duration_confirmed.is_(True),
                Task.planning_state == "ready",
            )
        )
        today_tasks = [
            task for task in result.scalars().all()
            if ensure_aware(task.deadline, now.tzinfo).date() == now.date()
            and is_meaningful_task(task)
        ]
        planned = sum(max(5, task.estimated_minutes or 0) for task in today_tasks)
        available = max(0, round((end_of_day - now).total_seconds() / 60))
        overflow = planned - available
        if overflow < 60 or not today_tasks:
            return None
        largest = max(today_tasks, key=lambda task: task.estimated_minutes or 0)
        return {
            "text": (
                f"Сегодня до 21:00 осталось {available} мин, а задач со сроком сегодня — "
                f"на {planned} мин. Не помещается около {overflow} мин.\n\n"
                f"Реши, переносить ли: {largest.title}"
            ),
            "related_entities": [{"type": "task", "id": largest.id}],
        }

    async def _stale_project_warning(self, user_id: int, session, now) -> dict | None:
        result = await session.execute(
            select(Project).where(
                and_(
                    Project.user_id == user_id,
                    Project.status == "active",
                    Project.updated_at <= now - timedelta(days=7),
                )
            ).order_by(Project.updated_at.asc()).limit(5)
        )
        for project in result.scalars().all():
            task_result = await session.execute(
                select(Task).where(
                    Task.user_id == user_id,
                    Task.status == "active",
                    Task.project_id == project.id,
                    Task.workflow_state != "blocked",
                ).order_by(Task.id.asc()).limit(1)
            )
            task = task_result.scalar_one_or_none()
            if not task or not is_meaningful_task(task):
                continue
            stale_days = max(7, (now - ensure_aware(project.updated_at, now.tzinfo)).days)
            action = task_action(task) or task.title
            return {
                "text": (
                    f"Проект «{project.title}» не менялся {stale_days} дней.\n\n"
                    f"Текущий шаг: {action}\n"
                    "Если проект больше не нужен, лучше закрыть его обычной фразой."
                ),
                "related_entities": [{"type": "task", "id": task.id}],
            }
        return None
