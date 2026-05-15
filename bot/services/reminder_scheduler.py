from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.database.models import Reminder
from bot.database.queries import get_all_active_reminders
from bot.keyboards.inline import reminder_keyboard

logger = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(self, tz, bot, session_factory):
        self.scheduler = AsyncIOScheduler(timezone=tz)
        self.bot = bot
        self.session_factory = session_factory
        self.tz = tz

    async def start_scheduler(self):
        if not self.scheduler.running:
            self.scheduler.start()
        async with self.session_factory() as session:
            reminders = await get_all_active_reminders(session)
        for reminder in reminders:
            self.schedule_reminder(reminder)

    def shutdown_scheduler(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _normalize_dt(self, dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=self.tz)

    def schedule_reminder(self, reminder: Reminder):
        remind_at = self._normalize_dt(reminder.remind_at)
        now = datetime.now(tz=self.tz)
        if remind_at <= now:
            return
        if not self.scheduler.running:
            logger.error("Scheduler not running; reminder %s saved but not scheduled", reminder.id)
            return
        self.scheduler.add_job(
            self.send_reminder,
            "date",
            run_date=remind_at,
            args=[reminder.id],
            id=f"reminder:{reminder.id}",
            replace_existing=True,
        )

    async def reschedule_reminder(self, reminder_id: int, new_remind_at: datetime):
        new_remind_at = self._normalize_dt(new_remind_at)
        async with self.session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            if not reminder:
                logger.warning("Reminder not found for reschedule: %s", reminder_id)
                self.cancel_reminder_job(reminder_id)
                return None
            reminder.remind_at = new_remind_at
            reminder.status = "active"
            await session.commit()
            await session.refresh(reminder)
        self.cancel_reminder_job(reminder_id)
        self.schedule_reminder(reminder)
        return reminder

    def cancel_reminder_job(self, reminder_id: int):
        job = self.scheduler.get_job(f"reminder:{reminder_id}")
        if job:
            job.remove()

    async def send_reminder(self, reminder_id: int):
        try:
            async with self.session_factory() as session:
                reminder = await session.get(Reminder, reminder_id)
                if not reminder:
                    logger.warning("Reminder not found for job reminder:%s", reminder_id)
                    self.cancel_reminder_job(reminder_id)
                    return
                if reminder.status != "active":
                    return
            await self.bot.send_message(reminder.user_id, f"Напоминание:\n\n{reminder.text}", reply_markup=reminder_keyboard(reminder.id))
        except Exception as exc:
            logger.exception("Failed to send reminder %s: %s", reminder_id, exc)
