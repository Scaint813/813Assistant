from __future__ import annotations

import unittest
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import (
    Base,
    Reminder,
    ScheduleOverride,
    Task,
    TaskPlanBlock,
    TrainingProfile,
    UserProfile,
    WorkoutSession,
)
from bot.middlewares import AccessMiddleware
from bot.services.ai_service import AIService
from bot.services.calendar_service import CalendarService
from bot.services.day_planning_service import DayPlanningService
from bot.services.intent_parser import IntentParser
from bot.services.preferences_service import PreferencesService
from bot.services.reminder_scheduler import ReminderScheduler
from bot.services.time_service import TimeService
from bot.services.user_profile_service import UserProfileService


class FixedTimeService(TimeService):
    def now(self) -> datetime:
        # One absolute instant: 23:30 in Moscow and already 01:30 next day in Almaty.
        return datetime(2026, 8, 3, 23, 30, tzinfo=self.tz)


class RecordingScheduler:
    running = True

    def __init__(self):
        self.jobs = []

    def add_job(self, *args, **kwargs):
        self.jobs.append((args, kwargs))


class ProfileAndPlanningTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_reminder_text_uses_each_users_local_date_and_offset(self):
        async with self.sessions() as session:
            session.add_all([
                UserProfile(user_id=1, timezone="Europe/Moscow"),
                UserProfile(user_id=2, timezone="Asia/Almaty"),
            ])
            await session.commit()

        parser = IntentParser(AIService("", "", ""), self.clock)
        moscow = await parser.parse_user_text(
            "Напомни сегодня в 23:45 позвонить врачу",
            context={"session_factory": self.sessions, "user_id": 1},
        )
        almaty = await parser.parse_user_text(
            "Напомни сегодня в 23:45 позвонить врачу",
            context={"session_factory": self.sessions, "user_id": 2},
        )

        moscow_at = datetime.fromisoformat(moscow["intents"][0]["remind_at"])
        almaty_at = datetime.fromisoformat(almaty["intents"][0]["remind_at"])
        self.assertEqual("Europe/Moscow", moscow["user_timezone"])
        self.assertEqual("Asia/Almaty", almaty["user_timezone"])
        self.assertEqual("2026-08-03T23:45:00+03:00", moscow_at.isoformat())
        self.assertEqual("2026-08-04T23:45:00+05:00", almaty_at.isoformat())

    async def test_scheduler_restores_naive_sqlite_time_in_reminder_timezone(self):
        scheduler = ReminderScheduler(
            self.tz, bot=None, session_factory=self.sessions, allowed_user_ids=(2,)
        )
        recorder = RecordingScheduler()
        scheduler.scheduler = recorder
        reminder = Reminder(
            id=7,
            user_id=2,
            text="Тренировка",
            remind_at=datetime(2030, 8, 4, 18, 20),
            timezone="Asia/Almaty",
        )

        scheduler.schedule_reminder(reminder)

        self.assertEqual(1, len(recorder.jobs))
        run_date = recorder.jobs[0][1]["run_date"]
        self.assertEqual("Asia/Almaty", run_date.tzinfo.key)
        self.assertEqual((18, 20), (run_date.hour, run_date.minute))

    async def test_direct_buttons_receive_the_users_local_clock(self):
        async with self.sessions() as session:
            session.add(UserProfile(user_id=2, timezone="Asia/Almaty"))
            await session.commit()

        received = {}

        async def handler(_event, data):
            received["now"] = data["time_service"].now()

        data = {
            "session_factory": self.sessions,
            "time_service": self.clock,
            "user_profile_service": UserProfileService(self.tz),
        }
        await AccessMiddleware((2,))(
            handler, SimpleNamespace(from_user=SimpleNamespace(id=2)), data
        )

        self.assertEqual("Asia/Almaty", received["now"].tzinfo.key)
        self.assertEqual("2026-08-04T01:30:00+05:00", received["now"].isoformat())

    async def test_large_workload_requires_confirmation_and_is_idempotent(self):
        now = datetime(2026, 8, 3, 9, 0, tzinfo=self.tz)
        calendar = CalendarService(self.clock)
        service = DayPlanningService(calendar)
        async with self.sessions() as session:
            profile = UserProfile(user_id=1, timezone="Europe/Moscow")
            PreferencesService().set(profile, "daily_focus_minutes", 180)
            PreferencesService().set(profile, "large_task_block_minutes", 90)
            PreferencesService().set(profile, "planning_weekends", False)
            session.add(profile)
            session.add(TrainingProfile(user_id=1, session_minutes=60))
            session.add(ScheduleOverride(
                user_id=1,
                date=now.date() + timedelta(days=1),
                mode="rest_day",
                create_tasks=False,
            ))
            session.add(WorkoutSession(
                user_id=1,
                title="Силовая",
                scheduled_for=now + timedelta(days=2),
            ))
            session.add(Task(
                user_id=1,
                title="Большой запуск",
                planning_state="ready",
                estimated_minutes=2400,
                duration_confirmed=True,
                deadline_confirmed=True,
                deadline=now + timedelta(days=20),
            ))
            await session.commit()

            preview = await service.build_overflow_proposal(session, 1, now)
            preview_count = await session.scalar(select(func.count(TaskPlanBlock.id)))
            self.assertEqual(0, preview_count)
            self.assertGreater(len(preview["proposal"]), 1)
            self.assertGreater(len({item["date"] for item in preview["proposal"]}), 1)
            self.assertNotIn(
                now.date() + timedelta(days=1),
                {item["date"] for item in preview["proposal"]},
            )
            self.assertTrue(all(item["date"].weekday() < 5 for item in preview["proposal"]))
            workout_day = [
                item for item in preview["proposal"]
                if item["date"] == now.date() + timedelta(days=2)
            ]
            self.assertTrue(workout_day)
            self.assertTrue(all(item["start"].hour >= 10 for item in workout_day))

            first = await service.apply_overflow_proposal(session, 1, now)
            await session.commit()
            first_count = await session.scalar(select(func.count(TaskPlanBlock.id)))
            blocks = list((await session.execute(select(TaskPlanBlock))).scalars())

            self.assertEqual(first["saved"], first_count)
            self.assertEqual(2310, sum(block.planned_minutes for block in blocks))
            self.assertLessEqual(max(block.planned_minutes for block in blocks), 90)
            daily_totals: dict = {}
            for block in blocks:
                daily_totals[block.plan_date] = (
                    daily_totals.get(block.plan_date, 0) + block.planned_minutes
                )
            self.assertLessEqual(max(daily_totals.values()), 180)

            second = await service.apply_overflow_proposal(session, 1, now)
            await session.commit()
            second_count = await session.scalar(select(func.count(TaskPlanBlock.id)))
            self.assertEqual(first_count, second_count)
            self.assertEqual(first["saved"], second["saved"])


if __name__ == "__main__":
    unittest.main()
