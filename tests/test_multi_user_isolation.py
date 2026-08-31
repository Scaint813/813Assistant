from __future__ import annotations

import unittest
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import _parse_allowed_user_ids
from bot.database.models import Base, Reminder, TrainingProfile
from bot.database.queries import (
    create_reminder,
    create_task,
    get_active_reminders,
    get_active_tasks,
)
from bot.middlewares import AccessMiddleware
from bot.services.checkin_service import CheckinService
from bot.services.focus_service import FocusService
from bot.services.reminder_scheduler import ReminderScheduler
from bot.services.training_service import TrainingService


class RecordingEvent:
    def __init__(self, user_id: int):
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str):
        self.answers.append(text)


class RecordingScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, *args, **kwargs):
        self.jobs.append((args, kwargs))


class RecordingBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, user_id, text, reply_markup=None):
        self.messages.append((user_id, text, reply_markup))


class MultiUserAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_allowlist_accepts_five_and_rejects_everyone_else(self):
        allowed = _parse_allowed_user_ids(101, "102, 103;104 105", limit=5)
        self.assertEqual((101, 102, 103, 104, 105), allowed)

        middleware = AccessMiddleware(allowed)
        handled = []

        async def handler(event, data):
            handled.append(event.from_user.id)

        owner_event = RecordingEvent(101)
        fifth_event = RecordingEvent(105)
        stranger_event = RecordingEvent(106)
        await middleware(handler, owner_event, {})
        await middleware(handler, fifth_event, {})
        await middleware(handler, stranger_event, {})

        self.assertEqual([101, 105], handled)
        self.assertEqual(["Доступ закрыт."], stranger_event.answers)

    async def test_allowlist_limit_is_enforced(self):
        with self.assertRaises(ValueError):
            _parse_allowed_user_ids(1, "2,3,4,5,6", limit=5)

    async def test_checkin_jobs_are_personal_for_all_five_users(self):
        scheduler = RecordingScheduler()
        service = CheckinService(
            scheduler=scheduler,
            session_factory=None,
            time_service=None,
            problem_block_service=None,
            next_step_service=None,
            overload_service=None,
            allowed_user_ids=(1, 2, 3, 4, 5),
            enabled=False,
            morning_time=time(9, 30),
            day_time=time(14, 30),
            evening_time=time(21, 30),
        )

        service.schedule_daily_checkins(RecordingBot())

        self.assertEqual(15, len(scheduler.jobs))
        job_ids = {kwargs["id"] for _, kwargs in scheduler.jobs}
        self.assertIn("checkin:morning:1", job_ids)
        self.assertIn("checkin:evening:5", job_ids)


class MultiUserDataIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tz = ZoneInfo("Europe/Moscow")

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_tasks_reminders_focus_and_training_profiles_do_not_cross_users(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=self.tz)
        training = TrainingService()
        async with self.sessions() as session:
            task_one = await create_task(session, 1, "Задача первого")
            task_two = await create_task(session, 2, "Задача второго")
            await create_reminder(session, 1, "Напоминание первого", now + timedelta(hours=1))
            await create_reminder(session, 2, "Напоминание второго", now + timedelta(hours=2))
            await training.save_profile(
                session, 1,
                "цель=сила; уровень=новичок; дни=пн,ср; минуты=40; "
                "оборудование=гантели; ограничения=нет",
            )
            await training.save_profile(
                session, 2,
                "цель=выносливость; уровень=средний; дни=вт,чт; минуты=30; "
                "оборудование=турник; ограничения=нет",
            )
            await session.commit()

        async with self.sessions() as session:
            self.assertEqual(["Задача первого"], [row.title for row in await get_active_tasks(session, 1)])
            self.assertEqual(["Задача второго"], [row.title for row in await get_active_tasks(session, 2)])
            self.assertEqual(
                ["Напоминание первого"],
                [row.text for row in await get_active_reminders(session, 1)],
            )
            self.assertEqual(
                ["Напоминание второго"],
                [row.text for row in await get_active_reminders(session, 2)],
            )
            profiles = list((await session.execute(select(TrainingProfile))).scalars().all())
            self.assertEqual({1, 2}, {profile.user_id for profile in profiles})

            # A crafted callback containing another user's task id must not
            # start or mutate that task.
            foreign_focus = await FocusService().start(session, 1, task_two.id, now, 25)
            self.assertIsNone(foreign_focus)
            own_focus = await FocusService().start(session, 1, task_one.id, now, 25)
            self.assertIsNotNone(own_focus)
            await session.commit()

    async def test_scheduler_never_delivers_to_user_removed_from_allowlist(self):
        bot = RecordingBot()
        scheduler = ReminderScheduler(self.tz, bot, self.sessions, allowed_user_ids=(1,))
        now = datetime(2026, 8, 1, 12, 0, tzinfo=self.tz)
        async with self.sessions() as session:
            allowed = Reminder(user_id=1, text="Разрешённое", remind_at=now)
            removed = Reminder(user_id=2, text="Не отправлять", remind_at=now)
            session.add_all([allowed, removed])
            await session.commit()
            allowed_id = allowed.id
            removed_id = removed.id

        await scheduler.send_reminder(removed_id)
        await scheduler.send_reminder(allowed_id)

        self.assertEqual([1], [message[0] for message in bot.messages])
