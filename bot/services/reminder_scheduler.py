from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler


class ReminderScheduler:
    def __init__(self, tz):
        self.scheduler = AsyncIOScheduler(timezone=tz)

    def start(self):
        self.scheduler.start()

    def schedule(self, run_date, func, *args, **kwargs):
        self.scheduler.add_job(func, "date", run_date=run_date, args=args, kwargs=kwargs)
