import json
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.database.queries import (
    archive_task,
    create_reminder,
    create_schedule_override,
    create_task,
    get_active_reminders,
    get_active_tasks,
    get_pending_preview,
)

router = Router()


@router.callback_query(F.data.startswith("confirm_preview:"))
async def confirm_preview(callback: CallbackQuery, session_factory):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if not preview or preview.status != "pending":
            await callback.message.answer("Preview не найден или уже обработан.")
            return
        payload = json.loads(preview.preview_json)
        for intent in payload.get("intents", []):
            t = intent.get("type")
            if t == "create_task":
                await create_task(session, callback.from_user.id, intent.get("title") or intent.get("text") or "Задача", description=intent.get("description") or "", priority=intent.get("priority") or "medium", is_minor=bool(intent.get("is_minor")), auto_cleanup_allowed=bool(intent.get("auto_cleanup_allowed")))
            elif t == "create_reminder":
                remind_at = datetime.fromisoformat(intent["remind_at"])
                await create_reminder(session, callback.from_user.id, intent.get("text") or "Напоминание", remind_at=remind_at, priority=intent.get("priority") or "medium")
            elif t in {"schedule_override", "rest_day"}:
                await create_schedule_override(session, callback.from_user.id, datetime.fromisoformat(intent["override_date"]).date(), mode=intent.get("mode") or "rest_day", create_tasks=bool(intent.get("create_tasks", False)), write_to_miro=bool(intent.get("write_to_miro", False)))
        preview.status = "confirmed"
        await session.commit()
    await callback.message.answer("Готово: изменения сохранены.")


@router.callback_query(F.data.startswith("cancel_preview:"))
async def cancel_preview(callback: CallbackQuery, session_factory):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if preview:
            preview.status = "cancelled"
            await session.commit()
    await callback.message.answer("Отменено. Ничего не записывал.")


@router.callback_query(F.data.startswith("edit_preview:"))
async def edit_preview(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Пришли исправленный текст сообщением — я подготовлю новый preview.")


@router.message(Command("cleanup"))
async def cleanup(message: Message, session_factory, cleanup_service, time_service):
    async with session_factory() as session:
        archived = await cleanup_service.run(session, message.from_user.id, time_service.now())
        await session.commit()
    if not archived:
        await message.answer("Убрано в архив: 0 задач")
        return
    await message.answer("Убрано в архив: {} задач\n{}".format(len(archived), "\n".join(f"- {t.title}" for t in archived)))


@router.message(Command("sync_miro"))
async def sync_miro(message: Message, session_factory, miro_service):
    if not miro_service.is_configured():
        await message.answer("Miro не настроен: отсутствует MIRO_ACCESS_TOKEN или MIRO_BOARD_ID")
        return
    async with session_factory() as session:
        tasks = await get_active_tasks(session, message.from_user.id)
        reminders = await get_active_reminders(session, message.from_user.id)
        for i, task in enumerate(tasks[:20]):
            item_id = await miro_service.create_or_update_task(task, i)
            if item_id and not task.miro_item_id:
                task.miro_item_id = item_id
        for i, reminder in enumerate(reminders[:20]):
            item_id = await miro_service.create_or_update_reminder(reminder, i)
            if item_id and not reminder.miro_item_id:
                reminder.miro_item_id = item_id
        await session.commit()
    await message.answer("Синхронизация Miro завершена.")
