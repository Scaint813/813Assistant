from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject


class PrivateUserOnlyMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_id: int):
        self.allowed_user_id = allowed_user_id

    async def __call__(self, handler, event: TelegramObject, data):
        message = event if isinstance(event, Message) else data.get("event_message")
        if message and message.from_user and message.from_user.id != self.allowed_user_id:
            await message.answer("⛔ Бот доступен только владельцу.")
            return None
        return await handler(event, data)
