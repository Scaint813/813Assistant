from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import (
    Base,
    PendingPreview,
    Project,
    Reminder,
    Task,
    UserRuntimeState,
)
from bot.handlers.domains import confirm_preview
from bot.services.action_log_service import ActionLogService
from bot.services.ai_service import AIService
from bot.services.assistant_ux_service import AssistantUXService
from bot.services.calendar_service import CalendarService
from bot.services.conversation_service import ConversationService
from bot.services.intent_parser import IntentParser
from bot.services.metric_service import MetricService
from bot.services.miro_sync_coordinator import MiroSyncCoordinator
from bot.services.next_step_service import NextStepService
from bot.services.project_service import ProjectService
from bot.services.reliability_service import ReliabilityService, render_system_notice
from bot.services.task_prioritization_service import TaskPrioritizationService
from bot.services.time_service import TimeService


class NoAI:
    def __init__(self):
        self.last_usage = {}

    async def parse_intent_with_openai(self, text, context):
        return None

    def parse_intent_fallback(self, text):
        return AIService.parse_intent_fallback(self, text)

    def parse_intent_fallback_batch(self, text):
        return AIService.parse_intent_fallback_batch(self, text)

    _clean_target = staticmethod(AIService._clean_target)
    _clean_project_request = staticmethod(AIService._clean_project_request)


class ConversationalUXTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tz = ZoneInfo("Europe/Moscow")
        self.clock = TimeService(self.tz, time(9), time(14), time(19), time(22))
        self.clock.now = lambda: datetime(2026, 8, 1, 12, 0, tzinfo=self.tz)
        self.parser = IntentParser(NoAI(), self.clock)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_natural_task_update_resolves_only_current_users_task(self):
        async with self.sessions() as session:
            own = Task(user_id=1, title="Подготовить документы", planning_state="inbox")
            other = Task(user_id=2, title="Подготовить документы", planning_state="inbox")
            session.add_all([own, other])
            await session.commit()

        parsed = await self.parser.parse_user_text(
            "Перенеси задачу подготовить документы на завтра, нужно 30 минут",
            {"session_factory": self.sessions, "user_id": 1},
        )
        intent = parsed["intents"][0]
        self.assertEqual("update_task", intent["type"])
        self.assertEqual(own.id, intent["entity_id"])
        self.assertEqual(30, intent["estimated_minutes"])
        self.assertTrue(intent["deadline"].startswith("2026-08-02"))
        self.assertNotEqual(other.id, intent["entity_id"])

    async def test_pick_task_phrase_is_read_only_and_never_becomes_create_task(self):
        parsed = await self.parser.parse_user_text(
            "Дай мне любую задачу",
            {"session_factory": self.sessions, "user_id": 1},
        )
        self.assertEqual(["pick_task"], [item["type"] for item in parsed["intents"]])
        self.assertEqual("deterministic", parsed["quality"]["parser_path"])

        async with self.sessions() as session:
            session.add(Task(
                user_id=1,
                title="Сверить договор",
                next_action="Открыть правки юриста",
                planning_state="ready",
                estimated_minutes=45,
                duration_confirmed=True,
            ))
            await session.commit()
            service = ConversationService(
                AssistantUXService(calendar_service=CalendarService(self.clock)),
                ProjectService(),
                NextStepService(),
            )
            reply = await service.answer(session, 1, "pick_task", self.clock.now())
            count = len((await session.execute(select(Task))).scalars().all())
        self.assertEqual(1, count)
        self.assertIn("Выбрал одну задачу из сохранённых", reply.text)
        self.assertIn("Сверить договор", reply.text)
        self.assertIn("Открыть правки юриста", reply.text)

    async def test_day_plan_phrase_is_read_only_query(self):
        parsed = await self.parser.parse_user_text(
            "Помоги составить план на день: что влезет, а что перенести",
            {"session_factory": self.sessions, "user_id": 1},
        )
        self.assertEqual(["plan_day"], [item["type"] for item in parsed["intents"]])
        self.assertEqual("deterministic", parsed["quality"]["parser_path"])

    async def test_planner_phrases_select_tomorrow_and_week(self):
        cases = {
            "Покажи расписание на сегодня": "show_today",
            "Что у меня завтра?": "show_tomorrow",
            "Покажи расписание на неделю": "show_week",
            "Составь план на неделю": "show_week",
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                parsed = await self.parser.parse_user_text(
                    phrase,
                    {"session_factory": self.sessions, "user_id": 1},
                )
                self.assertEqual([expected], [item["type"] for item in parsed["intents"]])
                self.assertEqual("deterministic", parsed["quality"]["parser_path"])

    async def test_task_duration_keeps_real_forty_hour_estimate(self):
        parsed = await self.parser.parse_user_text(
            "Подготовить запуск проекта завтра, нужно 40 часов",
            {"session_factory": self.sessions, "user_id": 1},
        )
        task = parsed["intents"][0]
        self.assertEqual("create_task", task["type"])
        self.assertEqual(2400, task["estimated_minutes"])
        self.assertTrue(task["duration_confirmed"])

    async def test_miro_background_sync_stays_off_while_integration_is_deferred(self):
        coordinator = MiroSyncCoordinator(
            miro_service=SimpleNamespace(is_configured=lambda: True),
            session_factory=self.sessions,
            time_service=self.clock,
            config=SimpleNamespace(allowed_user_id=1, miro_sync_enabled=False),
            next_step_service=SimpleNamespace(),
            delay_seconds=0,
        )
        coordinator.schedule(1)
        self.assertEqual({}, coordinator._tasks)

    async def test_last_shown_entity_supports_one_step_correction(self):
        async with self.sessions() as session:
            first = Task(user_id=1, title="Первое дело")
            second = Task(user_id=1, title="Второе дело")
            session.add_all([first, second])
            await session.flush()
            session.add(UserRuntimeState(user_id=1, last_entity_type="task", last_entity_id=first.id))
            await session.commit()

        parsed = await self.parser.parse_user_text(
            "Закрой эту задачу", {"session_factory": self.sessions, "user_id": 1}
        )
        self.assertEqual("complete_task", parsed["intents"][0]["type"])
        self.assertEqual(first.id, parsed["intents"][0]["entity_id"])

    async def test_multiline_voice_or_text_becomes_a_batch(self):
        parsed = await self.parser.parse_user_text(
            "Купить лекарства\nНапомни завтра в 18:20 позвонить врачу",
            {"session_factory": self.sessions, "user_id": 1},
        )
        self.assertEqual(["create_task", "create_reminder"], [item["type"] for item in parsed["intents"]])
        self.assertEqual("Купить лекарства", parsed["intents"][0]["source_text"])
        self.assertEqual("позвонить врачу", parsed["intents"][1]["text"])

    async def test_spoken_numbered_list_becomes_a_batch_without_ai(self):
        parsed = await self.parser.parse_user_text(
            "Первое, купить лекарства. Второе, напомни завтра в 18:20 позвонить врачу",
            {"session_factory": self.sessions, "user_id": 1},
        )
        self.assertEqual(["create_task", "create_reminder"], [item["type"] for item in parsed["intents"]])
        self.assertEqual("купить лекарства", parsed["intents"][0]["title"].casefold())

    async def test_natural_reminder_move_keeps_subject_and_changes_time(self):
        async with self.sessions() as session:
            reminder = Reminder(
                user_id=1, text="Поход к врачу",
                remind_at=self.clock.now() + timedelta(hours=2),
            )
            session.add(reminder)
            await session.commit()
        parsed = await self.parser.parse_user_text(
            "Перенеси напоминание поход к врачу на завтра в 19:00",
            {"session_factory": self.sessions, "user_id": 1},
        )
        intent = parsed["intents"][0]
        self.assertEqual("update_reminder", intent["type"])
        self.assertEqual(reminder.id, intent["entity_id"])
        self.assertTrue(intent["remind_at"].startswith("2026-08-02T19:00"))

    async def test_real_project_links_tasks_and_reports_progress(self):
        service = ProjectService()
        async with self.sessions() as session:
            project = await service.ensure(session, 1, "Запуск магазина", "Магазин принимает заказ")
            session.add_all([
                Task(user_id=1, title="Собрать каталог", project="Запуск магазина", project_id=project.id, status="done", completed_at=self.clock.now()),
                Task(user_id=1, title="Подключить оплату", project="Запуск магазина", project_id=project.id, next_action="Создать тестовый платёж"),
            ])
            await session.commit()
            text = await service.render_projects(session, 1, self.clock.now())
        self.assertIn("Прогресс: 1 из 2 (50%)", text)
        self.assertIn("Сейчас: Создать тестовый платёж", text)

    async def test_proactive_capacity_warning_uses_confirmed_minutes(self):
        async with self.sessions() as session:
            session.add_all([
                Task(
                    user_id=1, title="Подготовить презентацию", next_action="Собрать слайды",
                    deadline=self.clock.now().replace(hour=20), estimated_minutes=360,
                    duration_confirmed=True, deadline_confirmed=True, planning_state="ready",
                ),
                Task(
                    user_id=1, title="Сверить договор", next_action="Открыть правки",
                    deadline=self.clock.now().replace(hour=20), estimated_minutes=300,
                    duration_confirmed=True, deadline_confirmed=True, planning_state="ready",
                ),
            ])
            await session.commit()
            payload = await NextStepService().build_proactive_suggestion(1, session, self.clock.now())
        self.assertIn("Не помещается около 120 мин", payload["text"])
        self.assertEqual("task", payload["related_entities"][0]["type"])

    async def test_proactive_stale_project_names_real_next_action(self):
        async with self.sessions() as session:
            project = Project(
                user_id=1, title="Запуск магазина", objective="Получить первый заказ",
                updated_at=self.clock.now() - timedelta(days=9),
            )
            session.add(project)
            await session.flush()
            task = Task(
                user_id=1, title="Подключить оплату", next_action="Создать тестовый платёж",
                project="Запуск магазина", project_id=project.id,
            )
            session.add(task)
            await session.commit()
            payload = await NextStepService().build_proactive_suggestion(1, session, self.clock.now())
        self.assertIn("не менялся 9 дней", payload["text"])
        self.assertIn("Создать тестовый платёж", payload["text"])
        self.assertEqual(task.id, payload["related_entities"][0]["id"])

    async def test_undo_restores_updated_reminder(self):
        logs = ActionLogService()
        async with self.sessions() as session:
            reminder = Reminder(user_id=1, text="Врач", remind_at=self.clock.now() + timedelta(hours=2))
            session.add(reminder)
            await session.flush()
            before = {"text": reminder.text, "remind_at": reminder.remind_at, "status": reminder.status}
            reminder.remind_at = self.clock.now() + timedelta(days=1)
            await logs.log_update(
                session, 1, "reminder", reminder.id, "Перенос", before,
                {"remind_at": reminder.remind_at}, "batch", "text", undoable=True,
            )
            await session.commit()
            result = await logs.undo_batch(session, 1, "batch", self.clock.now())
            await session.commit()
            self.assertEqual(1, result["undone"])
            self.assertEqual(self.clock.now() + timedelta(hours=2), reminder.remind_at)
            self.assertIn(reminder.id, result["reschedule_reminder_ids"])

    async def test_confirm_applies_natural_task_update_and_offers_undo(self):
        import json

        async with self.sessions() as session:
            task = Task(user_id=1, title="Документы", planning_state="inbox")
            session.add(task)
            await session.flush()
            preview = PendingPreview(
                user_id=1,
                source_type="text",
                original_text="Перенеси задачу Документы на завтра, 30 минут",
                transcript="",
                preview_json=json.dumps({"intents": [{
                    "type": "update_task",
                    "entity_id": task.id,
                    "current_title": task.title,
                    "deadline": "2026-08-02T20:00:00+03:00",
                    "deadline_confirmed": True,
                    "estimated_minutes": 30,
                    "duration_confirmed": True,
                }]}),
            )
            session.add(preview)
            await session.commit()

        class Message:
            def __init__(self):
                self.edits = []
                self.answers = []

            async def edit_text(self, text, reply_markup=None):
                self.edits.append((text, reply_markup))

            async def answer(self, text, reply_markup=None):
                self.answers.append((text, reply_markup))

        class Callback:
            data = f"confirm_preview:{preview.id}"
            from_user = SimpleNamespace(id=1)

            def __init__(self):
                self.message = Message()

            async def answer(self):
                return None

        class Scheduler:
            def schedule_reminder(self, reminder):
                return None

            def cancel_reminder_job(self, reminder_id):
                return None

        class Metrics(MetricService):
            pass

        callback = Callback()
        await confirm_preview(
            callback=callback,
            session_factory=self.sessions,
            reminder_scheduler=Scheduler(),
            time_service=self.clock,
            problem_block_service=SimpleNamespace(),
            problem_resources_service=SimpleNamespace(),
            action_log_service=ActionLogService(),
            task_prioritization_service=TaskPrioritizationService(),
            project_service=ProjectService(),
            miro_sync_coordinator=SimpleNamespace(schedule=lambda user_id: None),
            metric_service=Metrics(),
        )
        async with self.sessions() as session:
            stored = await session.get(Task, task.id)
            self.assertEqual(30, stored.estimated_minutes)
            self.assertEqual("ready", stored.planning_state)
            self.assertEqual("2026-08-02 20:00", stored.deadline.strftime("%Y-%m-%d %H:%M"))
        self.assertIn("обновлена", callback.message.edits[0][0])
        self.assertIsNotNone(callback.message.edits[0][1])

    async def test_reliability_check_alerts_only_on_state_change_and_backup_is_valid(self):
        class Scheduler:
            running = True

            def get_job(self, job_id):
                return None

        class Bot:
            def __init__(self):
                self.messages = []

            async def send_message(self, user_id, text):
                self.messages.append((user_id, text))

        async with self.sessions() as session:
            session.add(Reminder(
                user_id=1, text="Врач",
                remind_at=self.clock.now() + timedelta(hours=2),
            ))
            await session.commit()

        with tempfile.TemporaryDirectory() as directory:
            source = f"{directory}/source.sqlite3"
            with sqlite3.connect(source) as database:
                database.execute("CREATE TABLE proof (value INTEGER)")
                database.execute("INSERT INTO proof VALUES (1)")
            service = ReliabilityService(
                Scheduler(), self.sessions, self.clock,
                f"sqlite+aiosqlite:///{source}", 1, (1,),
                f"{directory}/backups", 14, True,
            )
            bot = Bot()
            issues = await service.run_check(bot)
            await service.run_check(bot)
            backup = await service.create_backup(bot)
            self.assertIn("напоминание #1 не запланировано", issues)
            self.assertEqual(1, len(bot.messages))
            self.assertIn("Служебное уведомление 813Assistant", bot.messages[0][1])
            self.assertIn("Что произошло:", bot.messages[0][1])
            self.assertIn("Что делать:", bot.messages[0][1])
            self.assertTrue(backup and backup.exists())
            with sqlite3.connect(backup) as database:
                self.assertEqual(1, database.execute("SELECT value FROM proof").fetchone()[0])
            corrupt = Path(directory) / "assistant-corrupt.sqlite3"
            await asyncio.to_thread(corrupt.write_bytes, b"not-a-sqlite-backup")
            self.assertFalse(service.verify_backup(corrupt))

    async def test_quality_digest_explains_itself_without_operator_jargon(self):
        class Quality:
            async def evaluate_user(self, session, user_id, now, days):
                return {
                    "violations": ["p95 разбора 11812 мс (цель ≤8000 мс)"],
                }

        class Bot:
            def __init__(self):
                self.messages = []

            async def send_message(self, user_id, text):
                self.messages.append((user_id, text))

        service = ReliabilityService(
            SimpleNamespace(),
            self.sessions,
            self.clock,
            "sqlite+aiosqlite:///:memory:",
            1,
            (1,),
            "/tmp/unused-quality-backups",
            quality_service=Quality(),
        )
        bot = Bot()
        await service.send_quality_digest(bot)

        self.assertEqual(1, len(bot.messages))
        text = bot.messages[0][1]
        self.assertIn("⚙️ Служебное уведомление 813Assistant", text)
        self.assertIn("Это не задача и не напоминание", text)
        self.assertIn("95% разборов", text)
        self.assertIn("11.8 сек", text)
        self.assertNotIn("Quality SLO", text)
        self.assertNotIn("пользователь 1", text)

    def test_system_notice_has_predictable_sections(self):
        text = render_system_notice(
            icon="✅",
            title="Проверка завершена",
            summary="Всё работает.",
            impact="Нет влияния.",
            action="Ничего делать не нужно.",
        )
        self.assertLess(text.index("Что произошло:"), text.index("Влияние:"))
        self.assertLess(text.index("Влияние:"), text.index("Что делать:"))


if __name__ == "__main__":
    unittest.main()
