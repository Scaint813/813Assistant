from __future__ import annotations

import unittest
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, Task
from bot.database.queries import get_or_create_runtime_state
from bot.services.checkin_service import CheckinService
from bot.services.next_step_service import NextStepService
from bot.services.time_service import TimeService


class FixedTimeService(TimeService):
    def now(self):
        return getattr(self, "current", datetime(2026, 7, 31, 14, 30, tzinfo=self.tz))


class NoProblemBlocks:
    async def pick_checkin_problem_block(self, user_id, session, checkin_type, now):
        return None


class ConcreteNextStep:
    async def build_proactive_suggestion(self, user_id, session, now):
        return {
            "text": "Сегодня до 18:00 нужно:\nПодтвердить запись к врачу.",
            "related_entities": [{"type": "task", "id": 42}],
        }


class EmptyNextStep:
    async def build_proactive_suggestion(self, user_id, session, now):
        return None


class RecordingBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, user_id, text, reply_markup=None):
        self.messages.append((user_id, text, reply_markup))


class RecordingScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, *args, **kwargs):
        self.jobs.append((args, kwargs))


class CheckinQualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        tz = ZoneInfo("Europe/Moscow")
        self.clock = FixedTimeService(tz, time(9), time(14), time(19), time(22))

    async def asyncTearDown(self):
        await self.engine.dispose()

    def make_service(self, next_step_service):
        return CheckinService(
            scheduler=None,
            session_factory=self.sessions,
            time_service=self.clock,
            problem_block_service=NoProblemBlocks(),
            next_step_service=next_step_service,
            overload_service=None,
            allowed_user_id=1,
            enabled=True,
            morning_time=time(9, 30),
            day_time=time(14, 30),
            evening_time=time(21, 30),
        )

    async def test_checkin_has_one_real_action_and_marks_only_after_send(self):
        bot = RecordingBot()

        await self.make_service(ConcreteNextStep()).send_checkin(1, "day", bot)

        self.assertEqual(1, len(bot.messages))
        _, text, keyboard = bot.messages[0]
        self.assertIn("подтвердить запись к врачу", text.casefold())
        button_texts = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(["Готово", "Не присылать такие подсказки"], button_texts)
        async with self.sessions() as session:
            state = await get_or_create_runtime_state(session, 1)
            self.assertEqual(1, state.checkin_count_today)

    async def test_empty_checkin_is_not_sent_or_counted(self):
        bot = RecordingBot()

        await self.make_service(EmptyNextStep()).send_checkin(1, "evening", bot)

        self.assertEqual([], bot.messages)
        async with self.sessions() as session:
            state = await get_or_create_runtime_state(session, 1)
            self.assertEqual(0, state.checkin_count_today)

    async def test_disabled_default_can_be_enabled_without_restart(self):
        scheduler = RecordingScheduler()
        service = self.make_service(ConcreteNextStep())
        service.scheduler = scheduler
        service.enabled = False

        service.schedule_daily_checkins(RecordingBot())

        self.assertEqual(3, len(scheduler.jobs))
        async with self.sessions() as session:
            state = await get_or_create_runtime_state(session, 1)
            self.assertFalse(state.checkin_enabled)
            state.checkin_enabled = True
            await session.commit()
        async with self.sessions() as session:
            self.assertTrue(
                await service.should_send_checkin(
                    1, "day", session, self.clock.now()
                )
            )

    async def test_generic_undated_task_never_triggers_checkin(self):
        async with self.sessions() as session:
            session.add(Task(user_id=1, title="Задача", next_action="Задача"))
            await session.commit()
        bot = RecordingBot()

        await self.make_service(NextStepService()).send_checkin(1, "day", bot)

        self.assertEqual([], bot.messages)

    async def test_due_specific_task_explains_why_now_without_priority_jargon(self):
        async with self.sessions() as session:
            session.add(Task(
                user_id=1,
                title="Подготовить документы к врачу",
                next_action="Проверить список анализов",
                deadline=self.clock.now() + timedelta(hours=3),
            ))
            await session.commit()
        bot = RecordingBot()

        await self.make_service(NextStepService()).send_checkin(1, "day", bot)

        self.assertEqual(1, len(bot.messages))
        text = bot.messages[0][1]
        self.assertIn("Сегодня до 17:30", text)
        self.assertIn("Подготовить документы к врачу", text)
        self.assertIn("Начать с: Проверить список анализов", text)
        self.assertNotIn("приоритет", text.casefold())
        self.assertNotIn("score", text.casefold())

    async def test_same_task_is_not_repeated_within_72_hours(self):
        bot = RecordingBot()
        service = self.make_service(ConcreteNextStep())
        await service.send_checkin(1, "day", bot)
        self.clock.current = self.clock.now() + timedelta(days=1)

        await service.send_checkin(1, "day", bot)

        self.assertEqual(1, len(bot.messages))


if __name__ == "__main__":
    unittest.main()
