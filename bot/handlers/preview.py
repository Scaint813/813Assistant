from __future__ import annotations

import json
from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.database.queries import create_reminder, create_schedule_override, get_pending_preview, resolve_pending_preview

router = Router()


def _resolve_time_label(cfg, time_label: str):
    mapping = {
        "morning": cfg.morning_time,
        "day": cfg.day_time,
        "evening": cfg.evening_time,
        "night": cfg.night_time,
    }
    return mapping.get(time_label, cfg.day_time)


@router.callback_query(F.data == "preview:confirm")
async def confirm_preview(callback: CallbackQuery, session_factory, cfg, time_service):
    async with session_factory() as session:
        preview = await get_pending_preview(session, callback.from_user.id)
        if not preview:
            await callback.message.answer("Нет активного превью для подтверждения.")
            await callback.answer()
            return
        data = json.loads(preview.intents_json)
        for intent in data.get("intents", []):
            if intent.get("type") == "schedule_override":
                date = time_service.resolve_day(intent.get("date", "today"))
                await create_schedule_override(
                    session,
                    callback.from_user.id,
                    date=date,
                    mode=intent.get("mode", "normal"),
                    create_tasks=intent.get("create_tasks", True),
                    write_to_miro=intent.get("write_to_miro", False),
                )
            if intent.get("type") == "create_reminder":
                day = time_service.resolve_day(intent.get("date", "today"))
                t = _resolve_time_label(cfg, intent.get("time", "day"))
                remind_at = datetime.combine(day, t, tzinfo=cfg.timezone)
                await create_reminder(session, callback.from_user.id, intent.get("text", "Напоминание"), remind_at)
        await resolve_pending_preview(session, preview, "confirmed")
        await session.commit()
    await callback.message.answer("✅ Действия применены и сохранены.")
    await callback.answer()


@router.callback_query(F.data == "preview:cancel")
async def cancel_preview(callback: CallbackQuery, session_factory):
    async with session_factory() as session:
        preview = await get_pending_preview(session, callback.from_user.id)
        if preview:
            await resolve_pending_preview(session, preview, "cancelled")
            await session.commit()
    await callback.message.answer("Ок, ничего не сохранял.")
    await callback.answer()
