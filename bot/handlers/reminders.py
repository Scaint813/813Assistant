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
    lines = []
    for r in items[:20]:
        rel = f" ({r.related_entity_type}:{r.related_entity_id})" if r.related_entity_type and r.related_entity_id else ""
        lines.append(f"- {r.remind_at.strftime('%Y-%m-%d %H:%M')} — {r.text}{rel}")
    await message.answer("Активные напоминания:\n" + "\n".join(lines))
