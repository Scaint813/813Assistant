from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.database.queries import get_active_reminders, get_active_tasks

router = Router()


@router.message(Command("today"))
async def today_cmd(message: Message, session_factory, time_service):
    async with session_factory() as session:
        tasks = await get_active_tasks(session, message.from_user.id)
        reminders = await get_active_reminders(session, message.from_user.id)
    now = time_service.now()
    lines = [f"План на сегодня ({now.strftime('%d.%m.%Y')}):", ""]
    lines.append("Задачи:")
    lines.extend([f"- {t.title}" for t in tasks[:5]] or ["- Нет активных задач"])
    lines.append("")
    lines.append("Ближайшие напоминания:")
    lines.extend([f"- {r.remind_at.strftime('%H:%M')} {r.text}" for r in reminders] or ["- Нет активных напоминаний"])
    await message.answer("\n".join(lines))
