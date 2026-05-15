from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.database.queries import get_active_reminders

router = Router()


@router.message(Command("reminders"))
async def reminders(message: Message, session_factory, time_service):
    async with session_factory() as session:
        items = await get_active_reminders(session, message.from_user.id)
    if not items:
        await message.answer("Активных напоминаний нет.")
        return
    now = time_service.now()
    lines = ["Активные напоминания:\n"]
    for idx, r in enumerate(items[:20], start=1):
        overdue = " (просрочено)" if r.remind_at <= now else ""
        lines.append(f"{idx}. {r.text}\n   {r.remind_at.strftime('%Y-%m-%d %H:%M')} [{r.status}]{overdue}")
    await message.answer("\n".join(lines))
