from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import (
    Base,
    CalendarEvent,
    HealthSnapshot,
    Reminder,
    Task,
    TaskPlanBlock,
    WorkoutSession,
)
from bot.handlers.menu import _more_kb, help_cmd
from bot.handlers.start import WELCOME_TEXT
from bot.keyboards.main_menu import main_menu
from bot.services.action_preview import render_preview
from bot.services.assistant_ux_service import AssistantUXService
from bot.services.screen_service import ScreenService


class TelegramUXTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tz = ZoneInfo("Europe/Moscow")
        self.now = datetime(2026, 8, 1, 10, 0, tzinfo=self.tz)
        self.service = AssistantUXService()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def test_main_keyboard_uses_clear_frequent_actions(self):
        rows = [[button.text for button in row] for row in main_menu().keyboard]
        self.assertEqual(
            [
                ["Что у меня сегодня?", "Планнер"],
                ["Покажи все дела", "Профиль"],
                ["Что ты умеешь?"],
            ],
            rows,
        )
        self.assertFalse(main_menu().is_persistent)
        self.assertNotIn("Штаб", {item for row in rows for item in row})
        self.assertNotIn("Следующий шаг", {item for row in rows for item in row})

    async def test_confirmed_chat_cleanup_uses_bounded_telegram_batches(self):
        class FakeBot:
            def __init__(self):
                self.calls = []

            async def delete_messages(self, **kwargs):
                self.calls.append(kwargs)

        bot = FakeBot()
        await ScreenService().cleanup_recent_chat(
            bot, chat_id=77, through_message_id=250, history_window=205
        )

        self.assertEqual([100, 100, 5], [len(call["message_ids"]) for call in bot.calls])
        self.assertEqual(46, bot.calls[0]["message_ids"][0])
        self.assertEqual(250, bot.calls[-1]["message_ids"][-1])
        self.assertTrue(all(call["chat_id"] == 77 for call in bot.calls))

    async def test_chat_cleanup_isolates_one_undeletable_message(self):
        from aiogram.exceptions import TelegramBadRequest

        class FakeBot:
            def __init__(self):
                self.deleted = []

            async def delete_messages(self, **_kwargs):
                raise TelegramBadRequest(method=None, message="one old message")

            async def delete_message(self, chat_id, message_id):
                if message_id == 9:
                    raise TelegramBadRequest(method=None, message="too old")
                self.deleted.append((chat_id, message_id))

        bot = FakeBot()
        await ScreenService().cleanup_recent_chat(
            bot, chat_id=77, through_message_id=10, history_window=3
        )

        self.assertEqual([(77, 8), (77, 10)], bot.deleted)

    def test_more_menu_only_contains_useful_secondary_sections(self):
        labels = [button.text for row in _more_kb().inline_keyboard for button in row]
        self.assertEqual(
            ["Показать проекты", "Настроить подсказки", "Показать архив", "Открыть настройки"],
            labels,
        )
        self.assertNotIn("Обновить Miro", labels)
        self.assertNotIn("Здоровье", labels)

    def test_welcome_explains_input_with_examples(self):
        self.assertIn("напомни завтра в 18:20 сходить к врачу", WELCOME_TEXT)
        self.assertIn("составь мне план тренировок", WELCOME_TEXT)
        self.assertNotIn("Штаб открыт", WELCOME_TEXT)

    async def test_help_replaces_navigation_screen_instead_of_adding_message(self):
        class FakeMessage:
            def __init__(self):
                self.from_user = SimpleNamespace(id=7)
                self.chat = SimpleNamespace(id=11)
                self.answers = []

            async def answer(self, *args, **kwargs):
                self.answers.append((args, kwargs))

        class FakeScreenService:
            def __init__(self):
                self.rendered = []
                self.deleted_inputs = []

            async def render_screen(self, **kwargs):
                self.rendered.append(kwargs)

            async def delete_user_input(self, message):
                self.deleted_inputs.append(message)

        message = FakeMessage()
        screen_service = FakeScreenService()
        bot = object()

        await help_cmd(message, self.sessions, screen_service, bot)

        self.assertEqual([], message.answers)
        self.assertEqual(1, len(screen_service.rendered))
        self.assertEqual(7, screen_service.rendered[0]["user_id"])
        self.assertEqual(11, screen_service.rendered[0]["chat_id"])
        self.assertIn("Пиши обычным текстом", screen_service.rendered[0]["text"])
        self.assertEqual([message], screen_service.deleted_inputs)

    def test_problem_preview_states_what_will_be_saved(self):
        text = render_preview({"intents": [{
            "type": "create_problem_block",
            "title": "Не готов список документов",
            "problem_text": "Не знаю, какие анализы нужны врачу",
            "next_action": "Позвонить в клинику и запросить список",
        }]})
        self.assertIn("Что мешает: Не знаю, какие анализы нужны врачу", text)
        self.assertIn("Первый конкретный шаг: Позвонить в клинику", text)
        self.assertNotIn("СИТУАЦИЯ", text)
        self.assertNotIn("ВЫВОД", text)

    def test_task_preview_hides_internal_scores_and_repeated_fields(self):
        text = render_preview({"intents": [{
            "type": "create_task",
            "title": "Подготовить документы к врачу",
            "outcome": "Подготовить документы к врачу",
            "next_action": "Проверить список анализов",
            "importance": 5,
            "urgency": 5,
            "estimated_minutes": 30,
        }]})
        self.assertIn("Сохранить задачу?", text)
        self.assertIn("Начать с: Проверить список анализов", text)
        self.assertNotIn("Важность", text)
        self.assertNotIn("Срочность", text)
        self.assertNotIn("Результат:", text)

    def test_day_mode_preview_does_not_expose_deferred_integrations(self):
        text = render_preview({"intents": [{"type": "rest_day"}]})
        self.assertIn("Режим: день отдыха", text)
        self.assertNotIn("Miro", text)
        self.assertNotIn("write_to_miro", text)

    def test_user_previews_never_expose_internal_field_names(self):
        previews = [
            render_preview({"intents": [{
                "type": "create_task",
                "title": "Подготовить документы",
                "planning_state": "inbox",
                "priority": "medium",
            }]}),
            render_preview({"intents": [{
                "type": "create_reminder",
                "text": "Поход к врачу",
                "remind_at": "2026-08-01T18:20:00+03:00",
            }]}),
        ]
        forbidden = {"planning_state", "workflow_state", "priority", "medium", "callback_data"}
        for preview in previews:
            self.assertTrue(forbidden.isdisjoint(preview.split()))

    def test_reminder_preview_names_timezone_explicitly(self):
        text = render_preview({"intents": [{
            "type": "create_reminder",
            "text": "Поход к врачу",
            "remind_at": "2026-08-02T18:20:00+03:00",
            "timezone": "Europe/Moscow",
        }]})
        self.assertIn("Когда: 02.08.2026 18:20", text)
        self.assertIn("Часовой пояс: Europe/Moscow", text)

    async def test_today_uses_actual_tasks_reminders_health_and_workout(self):
        async with self.sessions() as session:
            session.add_all([
                Task(
                    user_id=1,
                    title="Подготовить документы к врачу",
                    next_action="Проверить список анализов",
                    priority="high",
                    importance=5,
                    urgency=5,
                    deadline=self.now + timedelta(hours=5),
                ),
                Reminder(
                    user_id=1,
                    text="Поход к врачу",
                    remind_at=self.now.replace(hour=18, minute=20),
                ),
                HealthSnapshot(
                    user_id=1,
                    date=date(2026, 8, 1),
                    steps=6400,
                    step_goal=10000,
                    workout_minutes=20,
                    sleep_minutes=430,
                ),
                WorkoutSession(
                    user_id=1,
                    scheduled_for=self.now + timedelta(days=1),
                    title="Силовая тренировка A",
                ),
            ])
            await session.commit()

        async with self.sessions() as session:
            screen = await self.service.today(session, 1, self.now)

        self.assertIn("Подготовить документы к врачу", screen.text)
        self.assertIn("Начать с: Проверить список анализов", screen.text)
        self.assertIn("18:20 — Поход к врачу", screen.text)
        self.assertIn("Шаги: 6 400 из 10 000", screen.text)
        self.assertIn("Силовая тренировка A", screen.text)
        self.assertEqual("task", screen.primary_entity["type"])

    async def test_planner_shows_today_tomorrow_and_week_without_moving_tasks(self):
        async with self.sessions() as session:
            scheduled = Task(
                user_id=1,
                title="Отправить документы",
                scheduled_start=self.now.replace(hour=11),
                scheduled_end=self.now.replace(hour=11, minute=30),
            )
            deadline = Task(
                user_id=1,
                title="Позвонить врачу",
                deadline=self.now.replace(hour=16) + timedelta(days=1),
            )
            planned = Task(user_id=1, title="Подготовить отчёт")
            undated = Task(user_id=1, title="Купить батарейки")
            session.add_all([scheduled, deadline, planned, undated])
            await session.flush()
            session.add_all([
                TaskPlanBlock(
                    user_id=1,
                    task_id=planned.id,
                    plan_date=(self.now + timedelta(days=3)).date(),
                    start_at=self.now.replace(hour=9) + timedelta(days=3),
                    end_at=self.now.replace(hour=10) + timedelta(days=3),
                    planned_minutes=60,
                ),
                Reminder(
                    user_id=1,
                    text="Забрать заказ",
                    remind_at=self.now.replace(hour=18, minute=20),
                ),
                CalendarEvent(
                    user_id=1,
                    external_id="doctor",
                    title="Приём у врача",
                    start_at=self.now.replace(hour=12) + timedelta(days=1),
                    end_at=self.now.replace(hour=13) + timedelta(days=1),
                ),
                WorkoutSession(
                    user_id=1,
                    title="Силовая тренировка",
                    scheduled_for=self.now.replace(hour=8) + timedelta(days=4),
                ),
            ])
            await session.commit()

        async with self.sessions() as session:
            today = await self.service.planner(session, 1, self.now, "today")
            tomorrow = await self.service.planner(session, 1, self.now, "tomorrow")
            week = await self.service.planner(session, 1, self.now, "week")
            persisted = await session.get(Task, scheduled.id)

        self.assertIn("Планнер · сегодня", today.text)
        self.assertIn("11:00–11:30 · Отправить документы", today.text)
        self.assertIn("🔔 18:20 · Забрать заказ", today.text)
        self.assertNotIn("Позвонить врачу", today.text)
        self.assertIn("Планнер · завтра", tomorrow.text)
        self.assertIn("📅 12:00–13:00 · Приём у врача", tomorrow.text)
        self.assertIn("до 16:00 · Позвонить врачу", tomorrow.text)
        self.assertIn("09:00–10:00 · Подготовить отчёт", week.text)
        self.assertIn("🏋️ 08:00 · Силовая тренировка", week.text)
        self.assertIn("Без даты и времени: 1", week.text)
        self.assertEqual(self.now.replace(hour=11), persisted.scheduled_start.replace(tzinfo=self.tz))

    async def test_recovery_plan_keeps_real_important_task(self):
        async with self.sessions() as session:
            session.add_all([
                Task(
                    user_id=2,
                    title="Отправить документы в клинику",
                    next_action="Прикрепить результаты анализов",
                    priority="urgent",
                    importance=5,
                    urgency=5,
                ),
                Task(
                    user_id=2,
                    title="Разобрать старые фотографии",
                    priority="low",
                    importance=1,
                    urgency=1,
                ),
            ])
            await session.commit()

        async with self.sessions() as session:
            screen = await self.service.recovery_plan(session, 2, self.now)

        self.assertIn("Отправить документы в клинику", screen.text)
        self.assertIn("Прикрепить результаты анализов", screen.text)
        self.assertNotIn("Ресурс просел", screen.text)
        self.assertNotIn("Закрыть один обязательный пункт", screen.text)


if __name__ == "__main__":
    unittest.main()
