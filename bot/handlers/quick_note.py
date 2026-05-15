from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.types import Message

from bot.database.queries import create_pending_preview, get_active_tasks, get_reminders_for_date, get_tasks_for_date, get_upcoming_overrides
from bot.keyboards.inline import confirm_keyboard
from bot.services.action_preview import render_preview

router = Router()


@router.message(F.text & ~F.text.startswith("/"))
async def capture_text(message: Message, session_factory, intent_parser, time_service):
    parsed = await intent_parser.parse_user_text(message.text, context={})
    first_intent = (parsed.get("intents") or [{}])[0].get("type")

    if first_intent == "do_nothing":
        await message.answer("Понял, ничего не записываю.")
        return

    if first_intent == "show_today":
        now = time_service.now()
        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
        day_end = day_start + timedelta(days=1)
        async with session_factory() as session:
            tasks = await get_tasks_for_date(session, message.from_user.id, day_start, day_end)
            reminders = await get_reminders_for_date(session, message.from_user.id, day_start, day_end)
            overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
        text = [f"Сегодня: {now.strftime('%Y-%m-%d %A')}", "", "Задачи:"]
        text.extend([f"- {t.title}" for t in tasks] or ["- нет"])
        text.append("\nНапоминания:")
        text.extend([f"- {r.remind_at.strftime('%H:%M')} {r.text}" for r in reminders] or ["- нет"])
        text.append("\nOverrides:")
        text.extend([f"- {o.date} {o.mode}" for o in overrides[:5]] or ["- нет"])
        await message.answer("\n".join(text))
        return

    if first_intent == "show_tasks":
        async with session_factory() as session:
            items = await get_active_tasks(session, message.from_user.id)
        await message.answer("Активные задачи:\n" + ("\n".join([f"- {t.title} [{t.priority}]" for t in items[:20]]) if items else "- нет"))
        return

    async with session_factory() as session:
        preview = await create_pending_preview(session, message.from_user.id, "text", message.text, message.text, parsed)
        await session.commit()
    await message.answer(render_preview(parsed), reply_markup=confirm_keyboard(preview.id))
