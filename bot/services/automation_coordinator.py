from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class AutomationCoordinator:
    """Own all user-scoped background jobs and quietly reconcile derived plans."""

    def __init__(
        self,
        scheduler,
        bot,
        checkin_service,
        assistant_loop_service,
        *,
        reconcile_hours: int = 4,
    ):
        self.scheduler = scheduler
        self.bot = bot
        self.checkin_service = checkin_service
        self.assistant_loop_service = assistant_loop_service
        self.reconcile_hours = max(1, int(reconcile_hours))
        self._timezones: dict[int, str] = {}

    def schedule(self, users: dict[int, str]) -> int:
        self._timezones = {
            int(user_id): str(timezone)
            for user_id, timezone in users.items()
        }
        for user_id, timezone in self._timezones.items():
            self.schedule_user(user_id, timezone)
        self.scheduler.add_job(
            self.rebuild_all,
            trigger="interval",
            hours=self.reconcile_hours,
            kwargs={"reason": "periodic_reconcile"},
            id="assistant:plan-reconcile",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        return len(self._timezones)

    def schedule_user(self, user_id: int, timezone: str) -> None:
        user_id = int(user_id)
        timezone = str(timezone)
        self._timezones[user_id] = timezone
        self.checkin_service.reschedule_user_checkins(user_id, self.bot, timezone)
        self.assistant_loop_service.schedule_user(
            self.scheduler,
            user_id,
            timezone,
        )

    async def profile_updated(self, user_id: int, timezone: str) -> None:
        """Apply a changed timezone to cron jobs without restarting the process."""
        self.schedule_user(user_id, timezone)
        logger.info(
            "User automation rescheduled: user=%s timezone=%s",
            user_id,
            timezone,
        )

    async def rebuild_all(self, reason: str = "startup") -> dict[int, dict]:
        if not self._timezones:
            return {}
        user_ids = tuple(self._timezones)
        results = await asyncio.gather(
            *(
                self.assistant_loop_service.rebuild_user(user_id, reason)
                for user_id in user_ids
            ),
            return_exceptions=True,
        )
        completed: dict[int, dict] = {}
        for user_id, result in zip(user_ids, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "Automatic plan reconcile failed: user=%s reason=%s",
                    user_id,
                    reason,
                    exc_info=(type(result), result, result.__traceback__),
                )
                continue
            completed[user_id] = result
        logger.info(
            "Automatic plan reconcile complete: reason=%s users=%s/%s",
            reason,
            len(completed),
            len(user_ids),
        )
        return completed
