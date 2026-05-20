from __future__ import annotations

from bot.keyboards.main_menu import MENU_TEXTS
from bot.keyboards.inline import confirm_keyboard, overload_keyboard
from bot.services.action_preview import render_preview
from bot.database.queries import (
    create_pending_preview,
    get_active_tasks,
    get_reminders_for_date,
    get_tasks_for_date,
    get_upcoming_overrides,
)

from datetime import datetime, timedelta
from aiogram import F, Router
from aiogram.types import Message

router = Router()


@router.message(F.text & ~F.text.startswith("/"))
async def capture_text(message: Message, session_factory, intent_parser, time_service, overload_service, screen_service):
    text = message.text or ""

    # ── Guard: bottom ReplyKeyboard texts ─────────────────────────────────────
    # Handled by menu.py (registered before quick_note). If we reach here anyway
    # with a menu text, silently drop — never goes to AI parser.
    if text in MENU_TEXTS:
        return

    # ── Overload detection ────────────────────────────────────────────────────
    overload = overload_service.detect_overload(text, {})
    if overload["is_overload"]:
        await message.answer(
            "ПЕРЕГРУЗ\n\nРесурс просел.\nСначала стабилизация, потом задачи.\n\n"
            "Рекомендация:\n1. Вода\n2. Еда\n3. 20–40 минут отдыха без телефона\n"
            "4. Проверить тело/боль\n5. Убрать одну необязательную задачу\n6. Сон в приоритет",
            reply_markup=overload_keyboard(),
        )
        await screen_service.delete_user_input(message)
        return

    # ── Intent parse ──────────────────────────────────────────────────────────
    parsed = await intent_parser.parse_user_text(
        text, context={"session_factory": session_factory, "user_id": message.from_user.id}
    )
    first_intent = (parsed.get("intents") or [{}])[0].get("type")

    if first_intent == "do_nothing":
        await message.answer("Принял. Ничего не фиксирую.")
        await screen_service.delete_user_input(message)
        return

    if first_intent == "show_today":
        now = time_service.now()
        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
        day_end = day_start + timedelta(days=1)
        async with session_factory() as session:
            tasks = await get_tasks_for_date(session, message.from_user.id, day_start, day_end)
            reminders = await get_reminders_for_date(session, message.from_user.id, day_start, day_end)
            overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
        lines = [f"Сегодня: {now.strftime('%Y-%m-%d %A')}", "", "Задачи:"]
        lines.extend([f"- {t.title}" for t in tasks] or ["- нет"])
        lines.append("\nНапоминания:")
        lines.extend([f"- {r.remind_at.strftime('%H:%M')} {r.text}" for r in reminders] or ["- нет"])
        if overrides:
            lines.append("\nРежим:")
            lines.extend([f"- {o.date} {o.mode}" for o in overrides[:3]])
        await message.answer("\n".join(lines))
        await screen_service.delete_user_input(message)
        return

    if first_intent == "show_tasks":
        async with session_factory() as session:
            items = await get_active_tasks(session, message.from_user.id)
        await message.answer("Активные задачи:\n" + (
            "\n".join([f"- {t.title} [{t.priority}]" for t in items[:20]]) if items else "- нет"
        ))
        await screen_service.delete_user_input(message)
        return

    # ── Action Preview ─────────────────────────────────────────────────────────
    # Delete user message AFTER preview is sent (text already read by parser)
    async with session_factory() as session:
        preview = await create_pending_preview(
            session, message.from_user.id, "text", text, text, parsed
        )
        await session.commit()
    await message.answer(render_preview(parsed), reply_markup=confirm_keyboard(preview.id))
    await screen_service.delete_user_input(message)
