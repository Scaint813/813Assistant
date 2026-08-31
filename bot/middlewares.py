from __future__ import annotations

from collections.abc import Iterable

from aiogram import BaseMiddleware

from bot.database.queries import touch_user_activity


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: int | Iterable[int]):
        if isinstance(allowed_user_ids, int):
            allowed_user_ids = (allowed_user_ids,)
        self.allowed_user_ids = frozenset(int(user_id) for user_id in allowed_user_ids)

    async def __call__(self, handler, event, data):
        from_user = getattr(event, "from_user", None)
        if self.allowed_user_ids and from_user and from_user.id not in self.allowed_user_ids:
            target = getattr(event, "message", None) or event
            if hasattr(target, "answer"):
                await target.answer("Доступ закрыт.")
            return
        session_factory = data.get("session_factory")
        time_service = data.get("time_service")
        if session_factory and from_user and time_service:
            async with session_factory() as session:
                profile_service = data.get("user_profile_service")
                if profile_service and hasattr(time_service, "in_timezone"):
                    # Every handler in this update receives the user's clock.
                    # This covers direct buttons (snooze, workouts, today, etc.),
                    # not only free-text parsing and reminder creation.
                    profile = await profile_service.get(session, from_user.id)
                    time_service = time_service.in_timezone(
                        profile_service.valid_timezone(profile.timezone)
                    )
                    data["time_service"] = time_service
                await touch_user_activity(session, from_user.id, time_service.now())
                await session.commit()
        return await handler(event, data)
