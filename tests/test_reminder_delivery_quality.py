from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import ActionLog, Base, Reminder, ReminderDelivery
from bot.services.reminder_scheduler import ReminderScheduler


class RecordingBot:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls = 0
        self.messages = []

    async def send_message(self, user_id, text, reply_markup=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("temporary telegram failure")
        self.messages.append((user_id, text, reply_markup))
        return SimpleNamespace(message_id=1000 + self.calls)


class ReminderDeliveryQualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tz = ZoneInfo("Europe/Moscow")

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _reminder(self, *, delivered: bool = False) -> Reminder:
        now = datetime.now(tz=self.tz)
        async with self.sessions() as session:
            reminder = Reminder(
                user_id=1,
                text="Поход к врачу",
                remind_at=now - timedelta(minutes=5),
                last_delivered_at=now if delivered else None,
            )
            session.add(reminder)
            await session.commit()
            await session.refresh(reminder)
            return reminder

    async def test_same_occurrence_is_delivered_only_once(self):
        reminder = await self._reminder()
        bot = RecordingBot()
        scheduler = ReminderScheduler(self.tz, bot, self.sessions, allowed_user_ids=(1,))

        await scheduler.send_reminder(reminder.id)
        await scheduler.send_reminder(reminder.id)

        self.assertEqual(1, len(bot.messages))
        async with self.sessions() as session:
            deliveries = list((await session.execute(select(ReminderDelivery))).scalars())
            log_count = await session.scalar(
                select(func.count(ActionLog.id)).where(ActionLog.action_type == "deliver")
            )
        self.assertEqual(1, len(deliveries))
        self.assertEqual("delivered", deliveries[0].status)
        self.assertEqual(1, log_count)

    async def test_failed_occurrence_can_retry_without_duplicate_success(self):
        reminder = await self._reminder()
        bot = RecordingBot(failures=1)
        scheduler = ReminderScheduler(self.tz, bot, self.sessions, allowed_user_ids=(1,))

        await scheduler.send_reminder(reminder.id)
        await scheduler.send_reminder(reminder.id)

        self.assertEqual(2, bot.calls)
        self.assertEqual(1, len(bot.messages))
        async with self.sessions() as session:
            delivery = await session.scalar(select(ReminderDelivery))
            stored = await session.get(Reminder, reminder.id)
        self.assertEqual("delivered", delivery.status)
        self.assertEqual(2, delivery.attempts)
        self.assertEqual(0, stored.delivery_attempts)

    async def test_stale_pending_delivery_is_quarantined_instead_of_duplicated(self):
        reminder = await self._reminder()
        bot = RecordingBot()
        scheduler = ReminderScheduler(self.tz, bot, self.sessions, allowed_user_ids=(1,))
        occurrence_key = ReminderScheduler._occurrence_key(
            reminder.id, scheduler._normalize_dt(reminder.remind_at)
        )
        async with self.sessions() as session:
            session.add(
                ReminderDelivery(
                    reminder_id=reminder.id,
                    user_id=reminder.user_id,
                    occurrence_key=occurrence_key,
                    scheduled_for=reminder.remind_at,
                    status="pending",
                    updated_at=datetime.now(tz=self.tz) - timedelta(minutes=15),
                )
            )
            await session.commit()

        await scheduler.send_reminder(reminder.id)

        async with self.sessions() as session:
            delivery = await session.scalar(select(ReminderDelivery))
        self.assertEqual(0, bot.calls)
        self.assertEqual("uncertain", delivery.status)
        self.assertIn("manual review", delivery.last_error)

    async def test_post_send_database_failure_is_not_retried_as_a_duplicate(self):
        reminder = await self._reminder()

        class FailThirdCommitFactory:
            def __init__(self, sessions):
                self.sessions = sessions
                self.calls = 0

            def __call__(self):
                self.calls += 1
                session = self.sessions()
                if self.calls == 3:
                    async def fail_commit():
                        raise RuntimeError("database failed after Telegram accepted")

                    session.commit = fail_commit
                return session

        bot = RecordingBot()
        scheduler = ReminderScheduler(
            self.tz,
            bot,
            FailThirdCommitFactory(self.sessions),
            allowed_user_ids=(1,),
        )

        await scheduler.send_reminder(reminder.id)

        async with self.sessions() as session:
            delivery = await session.scalar(select(ReminderDelivery))
            stored = await session.get(Reminder, reminder.id)
        self.assertEqual(1, bot.calls)
        self.assertEqual("uncertain", delivery.status)
        self.assertEqual(1001, delivery.telegram_message_id)
        self.assertEqual(0, stored.delivery_attempts)

    async def test_recent_missed_occurrence_is_recovered_after_restart(self):
        pending = await self._reminder()
        delivered = await self._reminder(delivered=True)
        bot = RecordingBot()
        scheduler = ReminderScheduler(self.tz, bot, self.sessions, allowed_user_ids=(1,))
        scheduler.scheduler.start()
        try:
            scheduler.schedule_reminder(pending, recover_overdue=True)
            scheduler.schedule_reminder(delivered, recover_overdue=True)
            self.assertIsNotNone(scheduler.scheduler.get_job(f"reminder:{pending.id}"))
            self.assertIsNone(scheduler.scheduler.get_job(f"reminder:{delivered.id}"))
        finally:
            scheduler.shutdown_scheduler()


if __name__ == "__main__":
    unittest.main()
