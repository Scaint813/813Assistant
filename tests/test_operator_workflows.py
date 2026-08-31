from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import ActionLog, Base, ProblemBlock, Reminder, ScheduleOverride, Task
from bot.services.action_log_service import ActionLogService
from bot.services.ai_service import AIService
from bot.services.intent_parser import IntentParser
from bot.services.reminder_scheduler import ReminderScheduler
from bot.services.task_prioritization_service import TaskPrioritizationService
from bot.services.time_service import TimeService


class FixedTimeService(TimeService):
    def now(self):
        return datetime(2026, 7, 31, 12, 0, tzinfo=self.tz)


class RecordingBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, user_id, text, reply_markup=None):
        self.messages.append((user_id, text, reply_markup))


class OperatorWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tz = ZoneInfo("Europe/Moscow")
        self.clock = FixedTimeService(
            self.tz, time(9), time(14), time(19), time(22)
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_recurring_reminder_keeps_subject_and_recurrence(self):
        parser = IntentParser(AIService("", "", ""), self.clock)
        result = await parser.parse_user_text(
            "Напомни каждый понедельник в 18:20: тренировка",
            context={},
        )

        reminder = result["intents"][0]
        self.assertEqual("create_reminder", reminder["type"])
        self.assertEqual("тренировка", reminder["text"])
        self.assertEqual("weekly", reminder["recurrence"])
        self.assertEqual(0, datetime.fromisoformat(reminder["remind_at"]).weekday())

    async def test_decomposition_fallback_is_structured_and_preserves_case(self):
        parser = IntentParser(AIService("", "", ""), self.clock)
        result = await parser.parse_user_text(
            "Разбей на шаги запуск Telegram MVP",
            context={},
        )

        plan = result["intents"][0]
        self.assertEqual("decompose_project", plan["type"])
        self.assertEqual("запуск Telegram MVP", plan["project"])
        self.assertGreaterEqual(len(plan["steps"]), 3)
        self.assertTrue(plan["steps"][1]["dependencies"])
        self.assertTrue(all(step["next_action"] for step in plan["steps"]))

    async def test_fact_based_order_builds_now_next_and_blocked(self):
        async with self.sessions() as session:
            session.add_all(
                [
                    Task(user_id=1, title="Критично", deadline=self.clock.now() + timedelta(hours=2)),
                    Task(user_id=1, title="Важная", deadline=self.clock.now() + timedelta(hours=6)),
                    Task(user_id=1, title="Средняя", deadline=self.clock.now() + timedelta(days=2)),
                    Task(user_id=1, title="Позже"),
                    Task(
                        user_id=1,
                        title="Ждём документ",
                        priority="urgent",
                        importance=5,
                        urgency=5,
                        blocked_reason="нет паспорта",
                    ),
                ]
            )
            await session.flush()
            ranked = await TaskPrioritizationService().refresh(
                session, 1, self.clock.now()
            )
            await session.commit()

        states = {task.title: task.workflow_state for task in ranked}
        self.assertEqual("now", states["Критично"])
        self.assertEqual("next", states["Позже"])
        self.assertEqual("blocked", states["Ждём документ"])
        self.assertIn("срок наступает", ranked[0].priority_reason)
        self.assertNotIn("score", ranked[0].priority_reason)
        self.assertNotIn("/5", ranked[0].priority_reason)

    async def test_undo_reverts_a_whole_confirm_batch(self):
        service = ActionLogService()
        now = self.clock.now()
        async with self.sessions() as session:
            task = Task(user_id=1, title="Задача")
            reminder = Reminder(
                user_id=1,
                text="Врач",
                remind_at=now + timedelta(hours=2),
            )
            block = ProblemBlock(user_id=1, title="Блок")
            override = ScheduleOverride(user_id=1, date=date(2026, 8, 1), mode="rest_day")
            session.add_all([task, reminder, block, override])
            await session.flush()
            for entity_type, entity, summary in (
                ("task", task, "Создана задача"),
                ("reminder", reminder, "Создано напоминание"),
                ("problem_block", block, "Создан блок"),
                ("schedule_override", override, "Создан режим"),
            ):
                await service.log_create(
                    session, 1, entity_type, entity.id, summary, "preview:42", "text"
                )
            result = await service.undo_batch(session, 1, "preview:42", now)
            await session.commit()

        async with self.sessions() as session:
            self.assertEqual(4, result["undone"])
            self.assertEqual(0, await session.scalar(select(func.count()).select_from(Task)))
            stored_reminder = await session.get(Reminder, reminder.id)
            stored_block = await session.get(ProblemBlock, block.id)
            self.assertEqual("cancelled", stored_reminder.status)
            self.assertEqual("archived", stored_block.status)
            self.assertIsNone(await session.get(ScheduleOverride, override.id))
            statuses = set(
                (await session.execute(select(ActionLog.status))).scalars().all()
            )
            self.assertEqual({"undone"}, statuses)

    async def test_scheduler_advances_a_daily_reminder_after_delivery(self):
        now = datetime.now(tz=self.tz)
        bot = RecordingBot()
        scheduler = ReminderScheduler(self.tz, bot, self.sessions)
        async with self.sessions() as session:
            reminder = Reminder(
                user_id=1,
                text="Выпить лекарство",
                remind_at=now - timedelta(minutes=1),
                recurrence="daily",
            )
            session.add(reminder)
            await session.commit()
            reminder_id = reminder.id

        await scheduler.send_reminder(reminder_id)

        async with self.sessions() as session:
            stored = await session.get(Reminder, reminder_id)
            self.assertGreater(
                scheduler._normalize_dt(stored.remind_at),
                datetime.now(tz=self.tz),
            )
            self.assertIsNotNone(stored.last_delivered_at)
            self.assertEqual("", stored.last_delivery_error)
        self.assertIn("Выпить лекарство", bot.messages[0][1])


if __name__ == "__main__":
    unittest.main()
