from __future__ import annotations

from aiogram import BaseMiddleware
from bot.database.queries import touch_user_activity


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_id: int):
        self.allowed_user_id = allowed_user_id

    async def __call__(self, handler, event, data):
        from_user = getattr(event, "from_user", None)
        if self.allowed_user_id and from_user and from_user.id != self.allowed_user_id:
            target = getattr(event, "message", None) or event
            if hasattr(target, "answer"):
                await target.answer("Доступ закрыт.")
            return
        session_factory = data.get("session_factory")
        time_service = data.get("time_service")
        if session_factory and from_user and time_service:
            async with session_factory() as session:
                await touch_user_activity(session, from_user.id, time_service.now())
                await session.commit()
        return await handler(event, data)
