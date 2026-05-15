from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.types import Message

from bot.database.queries import create_pending_preview, get_active_tasks, get_reminders_for_date, get_upcoming_overrides
from bot.keyboards.inline import confirm_keyboard
from bot.services.action_preview import render_preview

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.voice)
async def capture_voice(message: Message, bot, transcription_service, intent_parser, session_factory, time_service):
    if not transcription_service.api_key:
        await message.answer("Транскрибация не настроена: отсутствует OPENAI_API_KEY.")
        return

    temp_path: Path | None = None
    try:
        file = await bot.get_file(message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            temp_path = Path(tmp.name)
        await bot.download_file(file.file_path, str(temp_path))
    except Exception as exc:
        logger.exception("Voice download failed: %s", exc)
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

    parsed = await intent_parser.parse_user_text(
        transcript,
        context={"session_factory": session_factory, "user_id": message.from_user.id},
    )
    first_intent = (parsed.get("intents") or [{}])[0].get("type")

    if first_intent == "do_nothing":
        await message.answer("Расшифровка:\n" + transcript + "\n\nПонял, ничего не записываю.")
        return

    if first_intent == "show_today":
        # reuse text-flow immediate behavior via /today-like summary
        now = time_service.now()
        from datetime import datetime, timedelta

        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
        day_end = day_start + timedelta(days=1)
        async with session_factory() as session:
            tasks = await get_active_tasks(session, message.from_user.id)
            reminders = await get_reminders_for_date(session, message.from_user.id, day_start, day_end)
            overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
        text = [f"Сегодня: {now.strftime('%Y-%m-%d %A')}", "", "Задачи:"]
        text.extend([f"- {t.title}" for t in tasks[:20]] or ["- нет"])
        text.append("\nНапоминания:")
        text.extend([f"- {r.remind_at.strftime('%H:%M')} {r.text}" for r in reminders] or ["- нет"])
        text.append("\nOverrides:")
        text.extend([f"- {o.date} {o.mode}" for o in overrides[:5]] or ["- нет"])
        await message.answer("Расшифровка:\n" + transcript + "\n\n" + "\n".join(text))
        return


    if first_intent == "show_tasks":
        async with session_factory() as session:
            tasks = await get_active_tasks(session, message.from_user.id)
        body = "\n".join([f"- {t.title} [{t.priority}]" for t in tasks[:20]]) if tasks else "- нет"
        await message.answer("Расшифровка:\n" + transcript + "\n\nАктивные задачи:\n" + body)
        return

    async with session_factory() as session:
        preview = await create_pending_preview(session, message.from_user.id, "voice", transcript, transcript, parsed)
        await session.commit()

    await message.answer(f"Расшифровка:\n\"{transcript}\"\n\n{render_preview(parsed)}", reply_markup=confirm_keyboard(preview.id))
