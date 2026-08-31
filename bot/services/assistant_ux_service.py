from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import select

from bot.database.models import HealthSnapshot, Task, TrainingProfile, WorkoutSession
from bot.database.queries import (
    get_active_problem_blocks,
    get_active_reminders,
    get_or_create_runtime_state,
    get_upcoming_overrides,
)
from bot.services.content_quality import is_meaningful_task
from bot.services.datetime_utils import ensure_aware
from bot.services.task_prioritization_service import TaskPrioritizationService


@dataclass(slots=True)
class AssistantScreen:
    text: str
    primary_entity: dict | None = None
    entities: list[dict] = field(default_factory=list)


class AssistantUXService:
    """Build user-facing screens from real data without product jargon."""

    MONTHS = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    WEEKDAYS = (
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    )

    def __init__(
        self, prioritization_service: TaskPrioritizationService | None = None,
        calendar_service=None,
    ):
        self.prioritization_service = prioritization_service or TaskPrioritizationService()
        self.calendar_service = calendar_service

    async def today(self, session, user_id: int, now: datetime) -> AssistantScreen:
        tasks = [
            task for task in await self.prioritization_service.refresh(session, user_id, now)
            if is_meaningful_task(task)
        ]
        reminders = await get_active_reminders(session, user_id)
        blocks = await get_active_problem_blocks(session, user_id, now)
        overrides = await get_upcoming_overrides(session, user_id, now.date())
        state = await get_or_create_runtime_state(session, user_id)
        health = await self._latest_health(session, user_id)
        workout = await self._next_workout(session, user_id, now)
        day_plan = (
            await self.calendar_service.build_day_plan(session, user_id, now)
            if self.calendar_service else None
        )

        available = [
            task for task in tasks
            if task.workflow_state not in {"blocked", "inbox"}
        ]
        overdue = [
            task for task in available
            if task.deadline and ensure_aware(task.deadline, now.tzinfo) < now
        ]
        quiet_until = ensure_aware(state.quiet_until, now.tzinfo)
        day_mode = next(
            (
                override.mode
                for override in overrides
                if override.date == now.date()
            ),
            "normal",
        )

        lines = [f"План на {self._date_label(now)}", f"Время: {self._timezone_label(now)}"]
        if day_mode == "rest_day":
            lines += ["", "Сегодня день отдыха: оставляем только срочное."]
        if quiet_until and quiet_until > now:
            lines += ["", f"Плановые сообщения выключены до {quiet_until.strftime('%H:%M')}."]

        lines += ["", "План по времени:"]
        if day_plan and day_plan["timeboxes"]:
            for box in day_plan["timeboxes"][:5]:
                task = box["task"]
                lines.append(
                    f"{box['start'].strftime('%H:%M')}–{box['end'].strftime('%H:%M')} · "
                    f"{escape(task.title)}"
                )
                if box.get("is_partial"):
                    lines.append(
                        f"  Только блок {self._duration_label(box['planned_minutes'])} "
                        f"из общей оценки {self._duration_label(box['total_minutes'])}."
                    )
            if day_plan["unscheduled"]:
                lines.append(f"Не помещается сегодня: {len(day_plan['unscheduled'])}.")
            if day_plan["oversized"]:
                lines.append("Большие задачи продолжатся в следующие дни.")
        elif available:
            lines.append("Календарь не прислал свободные окна; вот ближайшие задачи:")
            for task in available[:3]:
                lines.append(f"• {escape(task.title)}")
                action = (task.next_action or "").strip()
                if action and action.casefold() != task.title.casefold():
                    lines.append(f"  Начать с: {escape(action)}")
        else:
            lines.append("Готовых к планированию задач нет.")

        if day_plan:
            if day_plan["events"]:
                lines += ["", "Занято в календаре:"]
                for event in day_plan["events"][:4]:
                    start = ensure_aware(event.start_at, now.tzinfo)
                    end = ensure_aware(event.end_at, now.tzinfo)
                    lines.append(f"• {start.strftime('%H:%M')}–{end.strftime('%H:%M')} · {escape(event.title)}")
            else:
                lines += ["", "Календарь: событий на сегодня нет или синхронизация ещё не подключена."]
            if day_plan["inbox"]:
                lines.append(f"Во Входящих ждут уточнения: {len(day_plan['inbox'])}.")

        future_reminders = [
            reminder for reminder in reminders
            if ensure_aware(reminder.remind_at, now.tzinfo) >= now
        ]
        lines += ["", "Ближайшие напоминания:"]
        if future_reminders:
            for reminder in future_reminders[:3]:
                lines.append(
                    f"• {self._datetime_label(reminder.remind_at, now)} — "
                    f"{escape(reminder.text)}"
                )
        else:
            lines.append("Нет запланированных.")

        facts = [f"активных задач: {len(tasks)}"]
        if overdue:
            facts.append(f"просрочено: {len(overdue)}")
        if blocks:
            facts.append(f"нерешённых проблем: {len(blocks)}")
        lines += ["", "Нагрузка: " + ", ".join(facts) + "."]

        if health and health.date == now.date():
            lines.append(f"Шаги: {health.steps:,} из {health.step_goal:,}.".replace(",", " "))
        if workout:
            lines.append(
                f"Следующая тренировка: {self._datetime_label(workout.scheduled_for, now)} — "
                f"{escape(workout.title)}."
            )
        if blocks:
            block = blocks[0]
            lines += [
                "",
                f"Требует решения: {escape(block.title)}",
                f"Следующее действие: {escape(block.next_action or 'уточнить первый шаг')}",
            ]

        if not tasks and not reminders:
            lines += [
                "",
                "Можно написать обычным сообщением:",
                "«напомни завтра в 18:20 сходить к врачу»",
                "или «разбей запуск проекта на шаги».",
            ]

        entity = self._task_entity(available[0]) if available else None
        return AssistantScreen("\n".join(lines), entity, [self._task_entity(task) for task in available[:3]])

    async def plan_day(self, session, user_id: int, now: datetime) -> AssistantScreen:
        """Explain what fits today, what does not, and how large tasks are split."""
        tasks = [
            task for task in await self.prioritization_service.refresh(session, user_id, now)
            if is_meaningful_task(task)
        ]
        if not self.calendar_service:
            return await self.today(session, user_id, now)

        plan = await self.calendar_service.build_day_plan(session, user_id, now)
        capacity = int(plan.get("capacity_minutes") or 0)
        remaining = int(plan.get("remaining_minutes") or 0)
        lines = [
            f"План дня · {self._date_label(now)}",
            f"Время: {self._timezone_label(now)}",
            "",
            f"Реальная вместимость до {plan['planning']['workday_end_hour']:02d}:00: "
            f"{self._duration_label(capacity)}.",
        ]
        if plan["calendar_free_minutes"] > capacity:
            lines.append(
                f"По часам свободно {self._duration_label(plan['calendar_free_minutes'])}, "
                "но план ограничен выбранным дневным фокусом."
            )
        if plan["events"]:
            lines.append(f"Учтено занятое время в календаре: {len(plan['events'])}.")
        if plan.get("workouts"):
            lines.append(f"Учтены запланированные тренировки: {len(plan['workouts'])}.")
        else:
            lines.append("Календарь не прислал занятые интервалы — расчёт без внешних событий.")

        lines += ["", "Взять сегодня:"]
        if plan["timeboxes"]:
            for index, box in enumerate(plan["timeboxes"][:6], 1):
                task = box["task"]
                lines.append(
                    f"{index}. {box['start'].strftime('%H:%M')}–{box['end'].strftime('%H:%M')} · "
                    f"{escape(task.title)}"
                )
                action = (task.next_action or "").strip()
                if action and action.casefold() != task.title.casefold():
                    lines.append(f"   Начать с: {escape(action)}")
                if box.get("is_partial"):
                    lines.append(
                        f"   Сегодня только {self._duration_label(box['planned_minutes'])} "
                        f"из {self._duration_label(box['total_minutes'])}."
                    )
        else:
            lines.append("Готовых задач для размещения нет.")

        if plan["oversized"]:
            lines += ["", "Большие задачи — не обещаю закрыть за день:"]
            for item in plan["oversized"][:3]:
                lines.append(
                    f"• {escape(item['task'].title)}: осталось после сегодняшнего блока "
                    f"{self._duration_label(item['remaining_minutes'])}; ориентир — "
                    f"около {item['estimated_days']} рабочих дней по "
                    f"{self._duration_label(item['daily_minutes'])}."
                )

        deferred = list(plan["unscheduled"])
        if deferred:
            lines += ["", "Не брать сегодня:"]
            for task in deferred[:5]:
                lines.append(f"• {escape(task.title)} — не помещается в свободные окна.")
            if len(deferred) > 5:
                lines.append(f"• И ещё {len(deferred) - 5}.")

        if plan["inbox"]:
            lines += [
                "",
                f"Нельзя честно оценить: {len(plan['inbox'])} задач без срока или длительности.",
                "Уточни их во Входящих — после этого пересчитаю план.",
            ]
        if tasks and not deferred and remaining:
            lines += ["", f"Запас после плана: {self._duration_label(remaining)}."]
        lines += ["", "Этот запрос ничего нового не создаёт: я выбираю только из сохранённых задач."]

        selected = [box["task"] for box in plan["timeboxes"]]
        entity = self._task_entity(selected[0]) if selected else None
        return AssistantScreen(
            "\n".join(lines),
            entity,
            [self._task_entity(task) for task in selected[:5]],
        )

    async def tasks(self, session, user_id: int, now: datetime) -> AssistantScreen:
        tasks = [
            task for task in await self.prioritization_service.refresh(session, user_id, now)
            if is_meaningful_task(task)
        ]
        current = [task for task in tasks if task.workflow_state == "now"]
        later = [task for task in tasks if task.workflow_state == "next"]
        blocked = [task for task in tasks if task.workflow_state == "blocked"]
        inbox = [task for task in tasks if task.planning_state == "inbox"]

        lines = ["Задачи"]
        if not tasks:
            lines += [
                "",
                "Пока задач нет.",
                "Напиши, например: «подготовить документы к пятнице, высокий приоритет».",
                "Для большого дела: «разбей запуск магазина на конкретные шаги».",
            ]
            return AssistantScreen("\n".join(lines))

        self._append_task_group(lines, "Сделать сейчас", current[:3], now)
        self._append_task_group(lines, "После этого", later[:6], now)
        self._append_task_group(lines, "Ждёт решения", blocked[:4], now, show_blocker=True)
        if inbox:
            lines += ["", "Входящие — нужно уточнить срок и длительность:"]
            lines.extend(f"{index}. {escape(task.title)}" for index, task in enumerate(inbox[:5], 1))
            lines.append("Чтобы уточнить их, напиши: «разберём входящие».")
        shown = min(3, len(current)) + min(6, len(later)) + min(4, len(blocked)) + min(5, len(inbox))
        if len(tasks) > shown:
            lines += ["", f"Ещё задач в базе: {len(tasks) - shown}."]
        lines += [
            "",
            "План строится из сроков, длительности и свободного времени — без скрытого рейтинга.",
        ]
        actionable = current + later
        entity = self._task_entity(actionable[0]) if actionable else None
        return AssistantScreen(
            "\n".join(lines),
            entity,
            [self._task_entity(task) for task in actionable[:5]],
        )

    async def reminders(self, session, user_id: int, now: datetime) -> AssistantScreen:
        reminders = await get_active_reminders(session, user_id)
        lines = ["Напоминания", f"Время: {self._timezone_label(now)}"]
        if not reminders:
            lines += [
                "",
                "Активных напоминаний нет.",
                "Пример: «напомни каждый понедельник в 18:20 про тренировку».",
            ]
            return AssistantScreen("\n".join(lines))

        entities = []
        for index, reminder in enumerate(reminders[:10], 1):
            remind_at = ensure_aware(reminder.remind_at, now.tzinfo)
            status = "просрочено" if remind_at < now else self._datetime_label(remind_at, now)
            recurrence = self._recurrence_label(reminder.recurrence)
            suffix = f" · {recurrence}" if recurrence else ""
            lines.append(f"\n{index}. {escape(reminder.text)}")
            lines.append(f"   {status}{suffix}")
            entities.append({"type": "reminder", "id": reminder.id, "index": index})
        if len(reminders) > 10:
            lines += ["", f"Ещё напоминаний: {len(reminders) - 10}."]
        return AssistantScreen("\n".join(lines), entities[0], entities)

    async def recovery_plan(
        self,
        session,
        user_id: int,
        now: datetime,
        *,
        sleep_problem: bool = False,
    ) -> AssistantScreen:
        tasks = [
            task for task in await self.prioritization_service.refresh(session, user_id, now)
            if is_meaningful_task(task)
        ]
        available = [task for task in tasks if task.workflow_state != "blocked"]
        must_do = [
            task for task in available
            if task.deadline and ensure_aware(task.deadline, now.tzinfo) <= now + timedelta(days=1)
        ]
        keep = (must_do or available)[:2]

        heading = "План на день после плохого сна" if sleep_problem else "Снижаем нагрузку"
        lines = [heading, "", "Оставляем только то, что действительно важно:"]
        if keep:
            for index, task in enumerate(keep, 1):
                lines.append(f"{index}. {escape(task.title)}")
                action = (task.next_action or task.title).strip()
                lines.append(f"   Начать с: {escape(action)}")
        else:
            lines.append("Срочных задач нет — можно взять паузу без чувства долга.")
        lines += [
            "",
            "Что убираем:",
            "• новые необязательные задачи;",
            "• переключение между несколькими делами;",
            "• решения, которые спокойно подождут до завтра.",
            "",
            "Если нужна пауза — нажми кнопку ниже. После неё вернись только к первому пункту.",
        ]
        entity = self._task_entity(keep[0]) if keep else None
        return AssistantScreen("\n".join(lines), entity, [self._task_entity(task) for task in keep])

    async def evening_review(self, session, user_id: int, now: datetime) -> AssistantScreen:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        completed_result = await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status.in_(["done", "archived"]),
                Task.completed_at.is_not(None),
                Task.completed_at >= day_start,
            )
        )
        completed = list(completed_result.scalars().all())
        active = [
            task for task in await self.prioritization_service.refresh(session, user_id, now)
            if is_meaningful_task(task)
        ]
        actionable = [task for task in active if task.workflow_state != "blocked"]
        lines = [
            "Итог дня",
            "",
            f"Завершено задач: {len(completed)}.",
            f"Осталось активных: {len(active)}.",
        ]
        if actionable:
            next_task = actionable[0]
            lines += [
                "",
                "Предлагаемый фокус на завтра:",
                escape(next_task.title),
                f"Начать с: {escape(next_task.next_action or next_task.title)}",
            ]
            entity = self._task_entity(next_task)
        else:
            lines += ["", "На завтра обязательных задач нет."]
            entity = None
        lines += ["", "Если срок нужно изменить, напиши это обычным сообщением."]
        return AssistantScreen("\n".join(lines), entity, [entity] if entity else [])

    async def wellbeing(self, session, user_id: int, now: datetime) -> AssistantScreen:
        health = await self._latest_health(session, user_id)
        profile_result = await session.execute(
            select(TrainingProfile).where(TrainingProfile.user_id == user_id)
        )
        profile = profile_result.scalar_one_or_none()
        workout = await self._next_workout(session, user_id, now)
        lines = ["Здоровье и тренировки", ""]
        if health:
            freshness = "сегодня" if health.date == now.date() else self._date_only_label(health.date)
            lines += [
                f"Данные Apple Health: {freshness}.",
                f"Шаги: {health.steps:,} из {health.step_goal:,}.".replace(",", " "),
                f"Тренировки: {health.workout_minutes} минут.",
                f"Сон: {health.sleep_minutes // 60} ч {health.sleep_minutes % 60} мин.",
            ]
            if health.resting_heart_rate:
                lines.append(f"Пульс покоя: {health.resting_heart_rate} уд/мин.")
            if health.hrv_ms:
                lines.append(f"HRV: {health.hrv_ms} мс.")
        else:
            lines += [
                "Данных Apple Health пока нет.",
                "Подключение выполняется через iPhone Shortcut; инструкция — /steps.",
            ]
        if workout:
            lines += [
                "",
                "Следующая тренировка:",
                f"{self._datetime_label(workout.scheduled_for, now)} — {escape(workout.title)}.",
            ]
        elif profile:
            lines += [
                "",
                f"Цель тренировок: {escape(profile.goal)}.",
                "Готового занятия впереди нет — напиши «составь план тренировок».",
            ]
        else:
            lines += [
                "",
                "Тренировочный профиль не заполнен.",
                "Напиши «составь план тренировок» — я по очереди соберу базовые вводные.",
            ]
        lines += [
            "",
            "В /training_plan можно начать занятие и записывать подходы по ходу.",
            "Перенос сначала проверяется на конфликты нагрузки.",
            "Быстрое завершение: /workout_done 7 короткая заметка.",
        ]
        return AssistantScreen("\n".join(lines))

    async def _latest_health(self, session, user_id: int) -> HealthSnapshot | None:
        result = await session.execute(
            select(HealthSnapshot)
            .where(HealthSnapshot.user_id == user_id)
            .order_by(HealthSnapshot.date.desc(), HealthSnapshot.captured_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _next_workout(self, session, user_id: int, now: datetime) -> WorkoutSession | None:
        result = await session.execute(
            select(WorkoutSession)
            .where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.status == "planned",
                WorkoutSession.scheduled_for >= now,
            )
            .order_by(WorkoutSession.scheduled_for.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    def _append_task_group(
        self,
        lines: list[str],
        title: str,
        tasks: list[Task],
        now: datetime,
        *,
        show_blocker: bool = False,
    ) -> None:
        if not tasks:
            return
        lines += ["", f"{title}:"]
        for index, task in enumerate(tasks, 1):
            lines.append(f"{index}. {escape(task.title)}")
            if show_blocker:
                lines.append(
                    f"   Нужно решить: {escape(task.blocked_reason or task.priority_reason or 'уточнить задачу')}"
                )
                continue
            action = (task.next_action or "").strip()
            if action and action.casefold() != task.title.casefold():
                lines.append(f"   Начать с: {escape(action)}")
            if task.deadline:
                lines.append(f"   Срок: {self._datetime_label(task.deadline, now)}")

    def _date_label(self, value: datetime) -> str:
        return f"{value.day} {self.MONTHS[value.month - 1]}, {self.WEEKDAYS[value.weekday()]}"

    def _date_only_label(self, value) -> str:
        return f"{value.day} {self.MONTHS[value.month - 1]}"

    def _datetime_label(self, value: datetime, now: datetime) -> str:
        value = ensure_aware(value, now.tzinfo)
        if value.date() == now.date():
            return f"сегодня в {value.strftime('%H:%M')}"
        if value.date() == (now + timedelta(days=1)).date():
            return f"завтра в {value.strftime('%H:%M')}"
        return f"{value.day} {self.MONTHS[value.month - 1]} в {value.strftime('%H:%M')}"

    @staticmethod
    def _timezone_label(now: datetime) -> str:
        name = getattr(now.tzinfo, "key", None) or str(now.tzinfo or "UTC")
        offset = now.strftime("%z")
        formatted_offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
        suffix = " · МСК" if name == "Europe/Moscow" else ""
        return f"{name} ({formatted_offset}{suffix})"

    @staticmethod
    def _duration_label(minutes: int) -> str:
        minutes = max(0, int(minutes or 0))
        hours, rest = divmod(minutes, 60)
        if hours and rest:
            return f"{hours} ч {rest} мин"
        if hours:
            return f"{hours} ч"
        return f"{rest} мин"

    @staticmethod
    def _recurrence_label(value: str | None) -> str:
        return {
            "daily": "каждый день",
            "weekdays": "по будням",
            "weekly": "каждую неделю",
            "monthly": "каждый месяц",
        }.get(value or "", "")

    @staticmethod
    def _task_entity(task: Task) -> dict:
        return {"type": "task", "id": task.id}
