from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import ClassVar

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, func, select

from bot.database.models import (
    ExamDate,
    ProblemBlock,
    Reminder,
    StudyScheduleItem,
    Task,
)
from bot.database.queries import (
    get_archived_tasks,
    get_done_reminders_count,
    get_or_create_runtime_state,
    remember_entity,
)

HELP_TEXT = (
    "Пиши или говори как человеку — команды не нужны.\n\n"
    "Можно одним сообщением:\n"
    "• «Завтра в 18:20 напомни про врача»;\n"
    "• «Добавь: отправить документы к пятнице, 30 минут»;\n"
    "• «Перенеси задачу про документы на завтра»;\n"
    "• «Задача с документами готова»;\n"
    "• «Дай мне одну задачу» — выбрать из уже сохранённых;\n"
    "• «Составь план на день: что влезет, а что перенести»;\n"
    "• «Что у меня завтра?» или «Покажи расписание на неделю»;\n"
    "• «Разбей запуск магазина на конкретные шаги»;\n"
    "• «Покажи проекты» или «подведи итоги недели».\n\n"
    "Если поручений несколько, перечисли их отдельными строками. "
    "Перед изменением данных я покажу точную проверку."
)


@dataclass(slots=True)
class ConversationReply:
    text: str
    reply_markup: InlineKeyboardMarkup | None = None


class ConversationService:
    QUERY_TYPES: ClassVar[frozenset[str]] = frozenset({
        "show_today", "show_tomorrow", "show_week", "show_tasks", "show_reminders", "show_projects",
        "show_inbox", "show_help", "show_weekly_review", "show_next",
        "pick_task", "plan_day",
        "show_problem_blocks",
        "show_archive", "show_study", "show_automations", "show_settings",
    })

    def __init__(self, assistant_ux_service, project_service, next_step_service):
        self.assistant_ux_service = assistant_ux_service
        self.project_service = project_service
        self.next_step_service = next_step_service

    async def answer(self, session, user_id: int, intent_type: str, now) -> ConversationReply | None:
        if intent_type == "show_today":
            screen = await self.assistant_ux_service.today(session, user_id, now)
            if screen.primary_entity:
                await remember_entity(
                    session, user_id, screen.primary_entity["type"], screen.primary_entity["id"]
                )
            return ConversationReply(screen.text)
        if intent_type in {"show_tomorrow", "show_week"}:
            period = "tomorrow" if intent_type == "show_tomorrow" else "week"
            screen = await self.assistant_ux_service.planner(
                session, user_id, now, period
            )
            if screen.primary_entity:
                await remember_entity(
                    session, user_id, screen.primary_entity["type"], screen.primary_entity["id"]
                )
            from bot.keyboards.inline import planner_keyboard

            return ConversationReply(screen.text, planner_keyboard(period))
        if intent_type == "plan_day":
            screen = await self.assistant_ux_service.plan_day(session, user_id, now)
            if screen.primary_entity:
                await remember_entity(
                    session, user_id, screen.primary_entity["type"], screen.primary_entity["id"]
                )
            from bot.keyboards.inline import day_plan_keyboard

            return ConversationReply(screen.text, day_plan_keyboard())
        if intent_type == "show_tasks":
            screen = await self.assistant_ux_service.tasks(session, user_id, now)
            if screen.entities:
                await remember_entity(session, user_id, "task", screen.entities[0]["id"])
            return ConversationReply(screen.text)
        if intent_type == "show_reminders":
            screen = await self.assistant_ux_service.reminders(session, user_id, now)
            if screen.entities:
                await remember_entity(session, user_id, "reminder", screen.entities[0]["id"])
            return ConversationReply(screen.text)
        if intent_type == "show_projects":
            return ConversationReply(await self.project_service.render_projects(session, user_id, now))
        if intent_type == "show_weekly_review":
            return ConversationReply(await self.project_service.weekly_review(session, user_id, now))
        if intent_type == "show_inbox":
            return ConversationReply(await self._inbox(session, user_id))
        if intent_type == "show_help":
            return ConversationReply(HELP_TEXT)
        if intent_type == "show_next":
            payload = await self.next_step_service.build_next_step(user_id, session, now)
            actions = payload.get("actions") or []
            if not actions:
                return ConversationReply("Сейчас нет конкретного следующего действия. Добавь дело обычным сообщением.")
            return ConversationReply("Что сделать сейчас\n\n" + "\n".join(f"{index}. {escape(action)}" for index, action in enumerate(actions[:3], 1)))
        if intent_type == "pick_task":
            payload = await self.next_step_service.pick_existing_task(user_id, session, now)
            if payload.get("status") != "selected":
                return ConversationReply(payload["text"])
            task = payload["task"]
            entity = payload["related_entity"]
            await remember_entity(session, user_id, entity["type"], entity["id"])
            from bot.keyboards.inline import next_step_entity_keyboard

            lines = [
                "Выбрал одну задачу из сохранённых",
                "",
                escape(task.title),
                f"Почему она: {escape(payload['reason'])}.",
                f"Сейчас: {escape(payload['action'])}.",
                f"Рабочий блок: {self._duration_label(payload['work_block_minutes'])}.",
            ]
            if payload["total_minutes"] > payload["work_block_minutes"]:
                lines.append(
                    f"Вся задача: {self._duration_label(payload['total_minutes'])}; "
                    "за один блок закрывать её не обещаю."
                )
            return ConversationReply("\n".join(lines), next_step_entity_keyboard(entity))
        if intent_type == "show_problem_blocks":
            result = await session.execute(
                select(ProblemBlock).where(
                    and_(ProblemBlock.user_id == user_id, ProblemBlock.status == "active")
                ).order_by(ProblemBlock.updated_at.desc()).limit(10)
            )
            blocks = list(result.scalars().all())
            if not blocks:
                return ConversationReply("Открытых препятствий нет.")
            lines = ["Открытые препятствия", ""]
            for block in blocks:
                lines.append(f"• {escape(block.title)}")
                if block.next_action:
                    lines.append(f"  Начать с: {escape(block.next_action)}")
            return ConversationReply("\n".join(lines))
        if intent_type == "show_archive":
            tasks = await get_archived_tasks(session, user_id)
            reminder_count = await get_done_reminders_count(session, user_id)
            if not tasks and reminder_count == 0:
                return ConversationReply("Архив пуст.")
            lines = [
                "Архив",
                "",
                f"Завершённых задач: {len(tasks)}",
                f"Завершённых напоминаний: {reminder_count}",
            ]
            if tasks:
                lines += ["", "Последние:"]
                lines.extend(f"• {escape(task.title)}" for task in tasks[:8])
            return ConversationReply("\n".join(lines))
        if intent_type == "show_study":
            exam_result = await session.execute(
                select(ExamDate).where(ExamDate.user_id == user_id, ExamDate.status == "active")
                .order_by(ExamDate.exam_date.asc())
            )
            schedule_result = await session.execute(
                select(StudyScheduleItem).where(
                    StudyScheduleItem.user_id == user_id,
                    StudyScheduleItem.status == "active",
                ).order_by(StudyScheduleItem.weekday.asc(), StudyScheduleItem.time_str.asc())
            )
            exams = list(exam_result.scalars().all())
            schedule = list(schedule_result.scalars().all())
            if not exams and not schedule:
                return ConversationReply(
                    "Учебных данных пока нет.\n\nНапиши, например: «ЕГЭ по русскому 3 июня» "
                    "или «репетитор по английскому по вторникам в 18:00»."
                )
            lines = ["Учёба", ""]
            if exams:
                lines.append("Экзамены:")
                lines.extend(f"• {escape(item.subject)} — {item.exam_date.strftime('%d.%m.%Y')}" for item in exams[:8])
            if schedule:
                lines += ["", "Занятия:"]
                lines.extend(f"• {escape(item.subject)} · {item.weekday} {escape(item.time_str)}" for item in schedule[:8])
            return ConversationReply("\n".join(lines))
        if intent_type == "show_automations":
            reminder_result = await session.execute(
                select(Reminder).where(
                    Reminder.user_id == user_id,
                    Reminder.status.in_(["active", "paused"]),
                ).order_by(Reminder.remind_at.asc()).limit(10)
            )
            reminders = list(reminder_result.scalars().all())
            state = await get_or_create_runtime_state(session, user_id)
            lines = ["Напоминания и подсказки", ""]
            if reminders:
                lines.extend(
                    f"• {escape(item.text)} · {item.remind_at.strftime('%d.%m %H:%M')}"
                    for item in reminders
                )
            else:
                lines.append("Активных напоминаний нет.")
            lines += ["", f"Контекстные подсказки: {'включены' if state.checkin_enabled else 'выключены'}. "]
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Настроить подсказки", callback_data="settings_checkins")
            ]])
            return ConversationReply("\n".join(lines).rstrip(), keyboard)
        if intent_type == "show_settings":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть профиль", callback_data="settings_back")
            ]])
            return ConversationReply(
                "Профиль и настройки\n\n"
                "Здесь меняются местное время, вместимость дня, check-ins и стиль ответов.",
                keyboard,
            )
        return None

    @staticmethod
    async def _inbox(session, user_id: int) -> str:
        result = await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status == "active",
                Task.planning_state == "inbox",
            ).order_by(Task.created_at.asc()).limit(5)
        )
        tasks = list(result.scalars().all())
        count_result = await session.execute(
            select(func.count(Task.id)).where(
                Task.user_id == user_id,
                Task.status == "active",
                Task.planning_state == "inbox",
            )
        )
        count = int(count_result.scalar_one() or 0)
        if not tasks:
            return "Входящие разобраны. Неполных задач нет."
        lines = [f"Входящие · {count}", ""]
        for task in tasks:
            missing = []
            if not task.duration_confirmed:
                missing.append("сколько займёт")
            if not task.deadline_confirmed:
                missing.append("когда нужно")
            lines.append(f"• {escape(task.title)} — уточнить: {', '.join(missing)}")
        lines += ["", "Можно исправить одной фразой: «В задаче про документы срок завтра, займёт 30 минут». "]
        return "\n".join(lines).rstrip()

    @staticmethod
    def _duration_label(minutes: int) -> str:
        hours, rest = divmod(max(0, int(minutes or 0)), 60)
        if hours and rest:
            return f"{hours} ч {rest} мин"
        return f"{hours} ч" if hours else f"{rest} мин"
