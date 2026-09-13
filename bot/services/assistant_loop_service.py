from __future__ import annotations

import logging
from datetime import time

logger = logging.getLogger(__name__)


class AssistantLoopService:
    """Keeps the derived task plan current when source facts change."""

    def __init__(
        self,
        day_planning_service,
        session_factory,
        time_service,
        user_profile_service,
        *,
        daily_rebuild_time: time = time(6, 45),
    ):
        self.day_planning_service = day_planning_service
        self.session_factory = session_factory
        self.time_service = time_service
        self.user_profile_service = user_profile_service
        self.daily_rebuild_time = daily_rebuild_time

    async def rebuild_in_session(self, session, user_id: int, now) -> dict:
        data = await self.day_planning_service.apply_overflow_proposal(
            session,
            user_id,
            now,
        )
        return {
            "saved": int(data.get("saved") or 0),
            "unallocated": len(data.get("unallocated") or []),
        }

    async def rebuild_user(self, user_id: int, reason: str = "scheduled") -> dict:
        async with self.session_factory() as session:
            profile = await self.user_profile_service.get(session, user_id)
            clock = self.time_service.in_timezone(
                self.user_profile_service.valid_timezone(profile.timezone)
            )
            result = await self.rebuild_in_session(session, user_id, clock.now())
            await session.commit()
        logger.info(
            "Assistant plan rebuilt: user=%s reason=%s saved=%s unallocated=%s",
            user_id,
            reason,
            result["saved"],
            result["unallocated"],
        )
        return result

    def schedule(self, scheduler, users: dict[int, str]) -> int:
        for user_id, timezone in users.items():
            self.schedule_user(scheduler, user_id, timezone)
        return len(users)

    def schedule_user(self, scheduler, user_id: int, timezone: str) -> None:
        scheduler.add_job(
            self.rebuild_user,
            trigger="cron",
            hour=self.daily_rebuild_time.hour,
            minute=self.daily_rebuild_time.minute,
            timezone=timezone,
            args=[int(user_id), "morning"],
            id=f"assistant:daily-plan:{int(user_id)}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
