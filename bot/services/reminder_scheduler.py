from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.keyboards.inline import reminder_keyboard


class ReminderScheduler:
    def __init__(self, tz, bot, session_factory):
        self.scheduler = AsyncIOScheduler(timezone=tz)
        self.bot = bot
        self.session_factory = session_factory

    def start(self):
        self.scheduler.start()

    async def load_from_db(self, reminders: list):
        for reminder in reminders:
            self.add_or_replace(reminder)

    def add_or_replace(self, reminder):
        self.scheduler.add_job(
            self._send_reminder,
            "date",
            run_date=reminder.remind_at,
            args=[reminder.user_id, reminder.id, reminder.text],
            id=f"reminder:{reminder.id}",
            replace_existing=True,
        )

    def remove(self, reminder_id: int):
        job_id = f"reminder:{reminder_id}"
        job = self.scheduler.get_job(job_id)
        if job:
            job.remove()

    async def _send_reminder(self, user_id: int, reminder_id: int, text: str):
        await self.bot.send_message(user_id, f"Напоминание:\n\n{text}", reply_markup=reminder_keyboard(reminder_id))
