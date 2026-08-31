from __future__ import annotations

from datetime import timedelta
from html import escape

from sqlalchemy import and_, or_, select

from bot.database.models import ProblemBlock, Project, Task
from bot.database.queries import get_active_projects, get_or_create_project
from bot.services.datetime_utils import ensure_aware


class ProjectService:
    async def ensure(self, session, user_id: int, title: str, objective: str = "") -> Project:
        return await get_or_create_project(session, user_id, title, objective)

    async def link_legacy_tasks(self, session, user_id: int) -> None:
        result = await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.project != "",
                Task.project_id.is_(None),
            )
        )
        for task in result.scalars().all():
            project = await self.ensure(session, user_id, task.project, task.project)
            task.project_id = project.id
        await session.flush()

    async def refresh(self, session, user_id: int) -> list[Project]:
        await self.link_legacy_tasks(session, user_id)
        projects = await get_active_projects(session, user_id)
        for project in projects:
            result = await session.execute(
                select(Task).where(
                    Task.user_id == user_id,
                    or_(Task.project_id == project.id, Task.project == project.title),
                ).order_by(Task.id.asc())
            )
            items = list(result.scalars().all())
            active = [item for item in items if item.status == "active"]
            next_task = next(
                (item for item in active if item.workflow_state != "blocked"),
                active[0] if active else None,
            )
            project.next_action = (
                (next_task.next_action or next_task.title)[:1000] if next_task else ""
            )
        await session.flush()
        return projects

    async def render_projects(self, session, user_id: int, now) -> str:
        projects = await self.refresh(session, user_id)
        if not projects:
            return (
                "Проектов пока нет.\n\n"
                "Напиши обычной фразой, например:\n"
                "«Разбей запуск магазина на конкретные шаги»."
            )
        lines = ["Проекты", ""]
        for project in projects[:10]:
            result = await session.execute(
                select(Task).where(
                    Task.user_id == user_id,
                    or_(Task.project_id == project.id, Task.project == project.title),
                )
            )
            items = list(result.scalars().all())
            active = [item for item in items if item.status == "active"]
            done = [item for item in items if item.status in {"done", "archived"}]
            blocked = [item for item in active if item.workflow_state == "blocked"]
            total = len(active) + len(done)
            progress = round(len(done) / total * 100) if total else 0
            lines += [
                escape(project.title),
                f"Прогресс: {len(done)} из {total} ({progress}%)",
            ]
            if project.objective and project.objective.casefold() != project.title.casefold():
                lines.append(f"Результат: {escape(project.objective[:180])}")
            if project.next_action:
                lines.append(f"Сейчас: {escape(project.next_action[:180])}")
            if blocked:
                lines.append(f"Заблокировано шагов: {len(blocked)}")
            if project.deadline:
                deadline = ensure_aware(project.deadline, now.tzinfo)
                lines.append(f"Срок: {deadline.strftime('%d.%m.%Y')}")
            lines.append("")
        lines.append("Чтобы изменить проект, просто назови его и нужное действие.")
        return "\n".join(lines)

    async def weekly_review(self, session, user_id: int, now) -> str:
        start = now - timedelta(days=7)
        task_result = await session.execute(
            select(Task).where(Task.user_id == user_id)
        )
        tasks = list(task_result.scalars().all())
        completed = [
            task for task in tasks
            if task.status == "done"
            and task.completed_at
            and ensure_aware(task.completed_at, now.tzinfo) >= start
        ]
        created = [task for task in tasks if ensure_aware(task.created_at, now.tzinfo) >= start]
        overdue = [
            task for task in tasks
            if task.status == "active" and task.deadline
            and ensure_aware(task.deadline, now.tzinfo) < now
        ]
        block_result = await session.execute(
            select(ProblemBlock).where(
                and_(ProblemBlock.user_id == user_id, ProblemBlock.status == "active")
            ).order_by(ProblemBlock.updated_at.desc())
        )
        blocks = list(block_result.scalars().all())
        projects = await self.refresh(session, user_id)

        lines = [
            "Итоги последних 7 дней",
            "",
            f"Завершено задач: {len(completed)}",
            f"Добавлено задач: {len(created)}",
            f"Активных проектов: {len(projects)}",
            f"Просрочено: {len(overdue)}",
            f"Открытых препятствий: {len(blocks)}",
        ]
        if completed:
            lines += ["", "Что сдвинулось:"]
            lines.extend(f"• {escape(task.title)}" for task in completed[:5])
        if overdue:
            lines += ["", "Что требует решения:"]
            for task in sorted(overdue, key=lambda item: item.deadline)[:3]:
                lines.append(f"• {escape(task.title)}")
        elif blocks:
            lines += ["", "Что требует решения:"]
            lines.extend(f"• {escape(block.title)}" for block in blocks[:3])
        active_ready = [
            task for task in tasks
            if task.status == "active" and task.planning_state == "ready"
            and task.workflow_state != "blocked"
        ]
        active_ready.sort(key=lambda item: (
            ensure_aware(item.deadline, now.tzinfo) if item.deadline else now + timedelta(days=3650),
            item.id,
        ))
        if active_ready:
            lines += ["", "Фокус следующей недели:"]
            for task in active_ready[:3]:
                lines.append(f"• {escape(task.next_action or task.title)}")
        lines += [
            "",
            "Это отчёт только по фактам из базы. Чтобы скорректировать план, напиши изменения обычным сообщением.",
        ]
        return "\n".join(lines)
