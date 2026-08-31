from __future__ import annotations

import logging
import tempfile
from html import escape
from pathlib import Path
from time import perf_counter

from aiogram import F, Router
from aiogram.types import Message

from bot.database.queries import create_pending_preview
from bot.keyboards.inline import confirm_keyboard
from bot.services.action_preview import render_preview

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.voice)
async def capture_voice(
    message: Message,
    bot,
    transcription_service,
    intent_parser,
    session_factory,
    time_service,
    screen_service,
    assistant_ux_service,
    conversation_service=None,
    metric_service=None,
    conversation_repair_service=None,
):
    if not transcription_service.api_key:
        await message.answer("Транскрибация не настроена: отсутствует OPENAI_API_KEY.")
        # Do NOT delete — user should know their voice wasn't understood
        return

    temp_path: Path | None = None
    try:
        file = await bot.get_file(message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            temp_path = Path(tmp.name)
        await bot.download_file(file.file_path, str(temp_path))
    except Exception:
        logger.exception("Voice download failed")
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        await message.answer("Не смог скачать голосовое. Попробуй ещё раз.")
        return

    try:
        transcript = await transcription_service.transcribe_audio(str(temp_path))
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)

    if transcript is None:
        await message.answer("Транскрибация временно недоступна. Напиши текстом.")
        return
    if not transcript.strip():
        await message.answer("Не смог разобрать голосовое. Попробуй ещё раз или напиши текстом.")
        return
    safe_transcript = escape(transcript.strip())
    repair_resolved = False
    parse_text = transcript
    if conversation_repair_service:
        async with session_factory() as session:
            parse_text, repair_resolved = await conversation_repair_service.merge_followup(
                session, message.from_user.id, transcript, time_service.now()
            )
            await session.commit()

    parse_started = perf_counter()
    parsed = await intent_parser.parse_user_text(
        parse_text,
        context={"session_factory": session_factory, "user_id": message.from_user.id},
    )
    quality = dict(parsed.get("quality") or {})
    quality.update({
        "source": "voice",
        "intent_count": len(parsed.get("intents") or []),
        "latency_ms": round((perf_counter() - parse_started) * 1000),
        "transcript_chars": len(transcript.strip()),
    })
    if metric_service:
        async with session_factory() as session:
            await metric_service.record(
                session, message.from_user.id, "intent_parsed",
                usage=parsed.get("usage"), metadata=quality,
            )
            if parsed.get("clarification"):
                if parsed.get("repair") and conversation_repair_service:
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
        await message.answer(
            f"Расшифровал так:\n«{safe_transcript}»\n\n"
            f"{escape(str(parsed['clarification']))}"
        )
        await screen_service.delete_user_input(message)
        return
    first_intent = (parsed.get("intents") or [{}])[0].get("type")
    intents = parsed.get("intents") or []
    timezone = parsed.get("user_timezone") or time_service.now().tzinfo
    user_clock = (
        time_service.in_timezone(timezone)
        if hasattr(time_service, "in_timezone")
        else time_service
    )

    if first_intent == "do_nothing":
        await message.answer(
            f"Расшифровал так:\n«{safe_transcript}»\n\nПонял, ничего не записываю."
        )
        await screen_service.delete_user_input(message)
        return

    if conversation_service and len(intents) == 1 and first_intent in conversation_service.QUERY_TYPES:
        async with session_factory() as session:
            reply = await conversation_service.answer(
                session, message.from_user.id, first_intent, user_clock.now()
            )
            can_render_screen = (
                hasattr(screen_service, "render_screen")
                and getattr(message, "chat", None) is not None
            )
            if can_render_screen:
                await screen_service.render_screen(
                    bot=bot,
                    session=session,
                    user_id=message.from_user.id,
                    chat_id=message.chat.id,
                    text=f"Расшифровал так:\n«{safe_transcript}»\n\n{reply.text}",
                    reply_markup=reply.reply_markup,
                )
            await session.commit()
        if not can_render_screen:
            await message.answer(
                f"Расшифровал так:\n«{safe_transcript}»\n\n{reply.text}",
                reply_markup=reply.reply_markup,
            )
        await screen_service.delete_user_input(message)
        return

    # Compatibility for isolated callers/tests that do not provide the new
    # conversation router yet.
    if first_intent in {"show_today", "show_tasks"}:
        async with session_factory() as session:
            screen = (
                await assistant_ux_service.today(session, message.from_user.id, user_clock.now())
                if first_intent == "show_today"
                else await assistant_ux_service.tasks(session, message.from_user.id, user_clock.now())
            )
            await session.commit()
        await message.answer(f"Расшифровал так:\n«{safe_transcript}»\n\n{screen.text}")
        await screen_service.delete_user_input(message)
        return

    # ── Action Preview ─────────────────────────────────────────────────────────
    # Voice file is already downloaded and transcript is read — safe to delete
    async with session_factory() as session:
        preview = await create_pending_preview(
            session, message.from_user.id, "voice", parse_text, transcript, parsed
        )
        if metric_service:
            await metric_service.record(
                session, message.from_user.id, "preview_created",
                entity_type="preview", entity_id=preview.id,
            )
        await session.commit()

    # Show transcript in preview so user sees what was understood
    preview_text = (
        f"Расшифровал так:\n«{safe_transcript}»\n\n"
        f"{escape(render_preview(parsed))}"
    )
    await message.answer(preview_text, reply_markup=confirm_keyboard(preview.id, parsed))
    await screen_service.delete_user_input(message)
