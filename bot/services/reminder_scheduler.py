from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.database.queries import get_all_active_reminders, get_reminder_by_id
from bot.keyboards.inline import reminder_keyboard

logger = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(self, tz, bot, session_factory):
        self.scheduler = AsyncIOScheduler(timezone=tz)
        self.bot = bot
        self.session_factory = session_factory

    async def start_scheduler(self):
        if not self.scheduler.running:
            self.scheduler.start()
        async with self.session_factory() as session:
            reminders = await get_all_active_reminders(session)
        for reminder in reminders:
            self.schedule_reminder(reminder)

    def schedule_reminder(self, reminder):
        now = datetime.now(tz=reminder.remind_at.tzinfo) if reminder.remind_at.tzinfo else datetime.utcnow()
        if reminder.remind_at <= now:
            return
        self.scheduler.add_job(
            self.send_reminder,
            "date",
            run_date=reminder.remind_at,
            args=[reminder.id],
            id=f"reminder:{reminder.id}",
            replace_existing=True,
        )

    async def reschedule_reminder(self, reminder_id: int, new_remind_at):
        async with self.session_factory() as session:
            reminder = await get_reminder_by_id(session, None, reminder_id) if False else None
        # rescheduling from handlers updates DB first, so this is job-only helper
        self.cancel_reminder_job(reminder_id)
        class _Tmp: pass
        tmp = _Tmp()
        tmp.id = reminder_id
        tmp.remind_at = new_remind_at
        self.schedule_reminder(tmp)

    def cancel_reminder_job(self, reminder_id: int):
        job = self.scheduler.get_job(f"reminder:{reminder_id}")
        if job:
            job.remove()

    async def send_reminder(self, reminder_id: int):
        try:
            async with self.session_factory() as session:
                from bot.database.models import Reminder
                from sqlalchemy import and_, select

                res = await session.execute(select(Reminder).where(and_(Reminder.id == reminder_id)))
                reminder = res.scalar_one_or_none()
                if not reminder:
                    logger.warning("Reminder not found for job reminder:%s", reminder_id)
                    self.cancel_reminder_job(reminder_id)
                    return
                if reminder.status != "active":
                    return
            await self.bot.send_message(reminder.user_id, f"Напоминание:\n\n{reminder.text}", reply_markup=reminder_keyboard(reminder.id))
        except Exception as exc:
            logger.exception("Failed to send reminder %s: %s", reminder_id, exc)
