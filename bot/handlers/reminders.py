from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.database.queries import get_active_reminders

router = Router()


@router.message(Command("reminders"))
async def reminders(message: Message, session_factory):
    async with session_factory() as session:
        items = await get_active_reminders(session, message.from_user.id)
    if not items:
        await message.answer("Активных напоминаний нет.")
        return
    text = "\n".join([f"- {r.remind_at}: {r.text}" for r in items[:20]])
    await message.answer(f"Активные напоминания:\n{text}")
