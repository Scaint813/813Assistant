from __future__ import annotations

from html import escape
from time import perf_counter

from aiogram import F, Router
from aiogram.types import Message

from bot.database.queries import create_pending_preview
from bot.keyboards.inline import confirm_keyboard
from bot.keyboards.main_menu import MENU_TEXTS
from bot.services.action_preview import render_preview

router = Router()


@router.message(F.text & ~F.text.startswith("/"))
async def capture_text(
    message: Message, session_factory, intent_parser, time_service, screen_service,
    metric_service, assistant_ux_service, conversation_service, conversation_repair_service,
):
    text = message.text or ""

    # ── Guard: bottom ReplyKeyboard texts ─────────────────────────────────────
    # Handled by menu.py (registered before quick_note). If we reach here anyway
    # with a menu text, silently drop — never goes to AI parser.
    if text in MENU_TEXTS:
        return

    async with session_factory() as session:
        text, repair_resolved = await conversation_repair_service.merge_followup(
            session, message.from_user.id, text, time_service.now()
        )
        await session.commit()

    # ── Intent parse ──────────────────────────────────────────────────────────
    parse_started = perf_counter()
    parsed = await intent_parser.parse_user_text(
        text, context={"session_factory": session_factory, "user_id": message.from_user.id}
    )
    quality = dict(parsed.get("quality") or {})
    quality.update({
        "source": "text",
        "intent_count": len(parsed.get("intents") or []),
        "latency_ms": round((perf_counter() - parse_started) * 1000),
    })
    async with session_factory() as session:
        await metric_service.record(
            session, message.from_user.id, "intent_parsed",
            usage=parsed.get("usage"), metadata=quality,
        )
        if parsed.get("clarification"):
            if parsed.get("repair"):
                await conversation_repair_service.remember(
                    session,
                    message.from_user.id,
                    parsed["repair"],
                    time_service.now(),
                )
            await metric_service.record(
                session,
                message.from_user.id,
                "clarification_requested",
                metadata=quality,
            )
        elif repair_resolved:
            await metric_service.record(
                session,
                message.from_user.id,
                "clarification_resolved",
                metadata=quality,
            )
        await session.commit()
    if parsed.get("clarification"):
        await message.answer(parsed["clarification"])
        await screen_service.delete_user_input(message)
        return
    first_intent = (parsed.get("intents") or [{}])[0].get("type")
    intents = parsed.get("intents") or []
    timezone = parsed.get("user_timezone") or time_service.tz
    user_clock = (
        time_service.in_timezone(timezone)
        if hasattr(time_service, "in_timezone")
        else time_service
    )

    if first_intent == "do_nothing":
        await message.answer("Принял. Ничего не фиксирую.")
        await screen_service.delete_user_input(message)
        return

    if len(intents) == 1 and first_intent in conversation_service.QUERY_TYPES:
        async with session_factory() as session:
            reply = await conversation_service.answer(
                session, message.from_user.id, first_intent, user_clock.now()
            )
            can_render_screen = (
                hasattr(screen_service, "render_screen")
                and getattr(message, "bot", None) is not None
                and getattr(message, "chat", None) is not None
            )
            if can_render_screen:
                await screen_service.render_screen(
                    bot=message.bot,
                    session=session,
                    user_id=message.from_user.id,
                    chat_id=message.chat.id,
                    text=reply.text,
                    reply_markup=reply.reply_markup,
                )
            await session.commit()
        if not can_render_screen:
            await message.answer(reply.text, reply_markup=reply.reply_markup)
        await screen_service.delete_user_input(message)
        return

    # ── Action Preview ─────────────────────────────────────────────────────────
    # Delete user message AFTER preview is sent (text already read by parser)
    async with session_factory() as session:
        preview = await create_pending_preview(
            session, message.from_user.id, "text", text, text, parsed
        )
        await metric_service.record(
            session, message.from_user.id, "preview_created",
            entity_type="preview", entity_id=preview.id,
        )
        await session.commit()
    await message.answer(
        escape(render_preview(parsed)),
        reply_markup=confirm_keyboard(preview.id, parsed),
    )
    await screen_service.delete_user_input(message)
