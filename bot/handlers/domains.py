import json
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.database.queries import (
    create_reminder,
    create_schedule_override,
    create_task,
    find_task_by_title,
    get_active_reminders,
    get_active_tasks,
    get_pending_preview,
    get_reminder_by_id,
)
from bot.keyboards.inline import reminder_snooze_keyboard


from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.callback_query(F.data.startswith("confirm_preview:"))
async def confirm_preview(callback: CallbackQuery, session_factory, reminder_scheduler):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if not preview or preview.status != "pending":
            await callback.message.answer("Preview не найден или уже обработан.")
            return
        payload = json.loads(preview.preview_json)
        created_tasks_by_title = {}
        for intent in payload.get("intents", []):
            t = intent.get("type")
            if t == "create_task":
                task = await create_task(
                    session,
                    callback.from_user.id,
                    intent.get("title") or intent.get("text") or "Задача",
                    description=intent.get("description") or "",
                    priority=intent.get("priority") or "medium",
                    is_minor=bool(intent.get("is_minor")),
                    auto_cleanup_allowed=bool(intent.get("auto_cleanup_allowed")),
                )
                created_tasks_by_title[task.title] = task
            elif t == "create_reminder":
                remind_at = datetime.fromisoformat(intent["remind_at"])
                related_entity_type = ""
                related_entity_id = None
                task_id = intent.get("task_id") or intent.get("related_entity_id")
                if task_id:
                    related_entity_type = "task"
                    related_entity_id = int(task_id)
                elif intent.get("task_title") and intent.get("task_title") in created_tasks_by_title:
                    related_entity_type = "task"
                    related_entity_id = created_tasks_by_title[intent.get("task_title")].id
                elif intent.get("task_title"):
                    existing = await find_task_by_title(session, callback.from_user.id, intent.get("task_title"))
                    if existing:
                        related_entity_type = "task"
                        related_entity_id = existing.id
                reminder = await create_reminder(
                    session,
                    callback.from_user.id,
                    intent.get("text") or "Напоминание",
                    remind_at=remind_at,
                    priority=intent.get("priority") or "medium",
                    related_entity_type=related_entity_type,
                    related_entity_id=related_entity_id,
                )
                reminder_scheduler.add_or_replace(reminder)
            elif t in {"schedule_override", "rest_day"}:
                await create_schedule_override(
                    session,
                    callback.from_user.id,
                    datetime.fromisoformat(intent["override_date"]).date(),
                    mode=intent.get("mode") or "rest_day",
                    create_tasks=bool(intent.get("create_tasks", False)),
                    write_to_miro=bool(intent.get("write_to_miro", False)),
                )
        preview.status = "confirmed"
        await session.commit()

        reply_text = "Готово, задача создана."
        if any(i.get("type") == "create_reminder" for i in payload.get("intents", [])):
            reply_text = "Готово, напоминание создано."
        elif any(i.get("type") in {"schedule_override", "rest_day"} for i in payload.get("intents", [])):
            reply_text = "Готово, день отдыха сохранён."
    await callback.message.answer(reply_text)

    await callback.message.answer("Готово, задача создана.")


@router.callback_query(F.data.startswith("cancel_preview:"))
async def cancel_preview(callback: CallbackQuery, session_factory):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if preview:
            preview.status = "cancelled"
            await session.commit()
    await callback.message.answer("Отменено.")


@router.callback_query(F.data.startswith("edit_preview:"))
async def edit_preview(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Пришли исправленный текст сообщением — я подготовлю новый preview.")


@router.callback_query(F.data.startswith("reminder_done:"))
async def reminder_done(callback: CallbackQuery, session_factory, reminder_scheduler):
    await callback.answer()
    reminder_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        reminder = await get_reminder_by_id(session, callback.from_user.id, reminder_id)
        if not reminder:
            await callback.message.answer("Напоминание не найдено.")
            return
        reminder.status = "done"
        await session.commit()
    reminder_scheduler.remove(reminder_id)
    await callback.message.answer("Отмечено как выполненное.")


@router.callback_query(F.data.startswith("reminder_cancel:"))
async def reminder_cancel(callback: CallbackQuery, session_factory, reminder_scheduler):
    await callback.answer()
    reminder_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        reminder = await get_reminder_by_id(session, callback.from_user.id, reminder_id)
        if not reminder:
            await callback.message.answer("Напоминание не найдено.")
            return
        reminder.status = "cancelled"
        await session.commit()
    reminder_scheduler.remove(reminder_id)
    await callback.message.answer("Напоминание отменено.")


@router.callback_query(F.data.startswith("reminder_snooze:"))
async def reminder_snooze(callback: CallbackQuery):
    await callback.answer()
    reminder_id = int(callback.data.split(":", 1)[1])
    await callback.message.answer("Выберите перенос:", reply_markup=reminder_snooze_keyboard(reminder_id))


async def _apply_snooze(callback: CallbackQuery, session_factory, reminder_scheduler, time_service, reminder_id: int, mode: str):
    async with session_factory() as session:
        reminder = await get_reminder_by_id(session, callback.from_user.id, reminder_id)
        if not reminder:
            await callback.message.answer("Напоминание не найдено.")
            return
        now = time_service.now()
        if mode == "1h":
            reminder.remind_at = now + timedelta(hours=1)
        elif mode == "evening":
            reminder.remind_at = time_service.build_datetime("today", "evening")
            if reminder.remind_at <= now:
                reminder.remind_at = time_service.build_datetime("tomorrow", "evening")
        else:
            reminder.remind_at = time_service.build_datetime("tomorrow", "morning")
        reminder.status = "active"
        await session.commit()
        reminder_scheduler.add_or_replace(reminder)
    await callback.message.answer(f"Перенесено на {reminder.remind_at.strftime('%Y-%m-%d %H:%M')}")


@router.callback_query(F.data.startswith("snooze_1h:"))
async def snooze_1h(callback: CallbackQuery, session_factory, reminder_scheduler, time_service):
    await callback.answer()
    await _apply_snooze(callback, session_factory, reminder_scheduler, time_service, int(callback.data.split(":", 1)[1]), "1h")


@router.callback_query(F.data.startswith("snooze_evening:"))
async def snooze_evening(callback: CallbackQuery, session_factory, reminder_scheduler, time_service):
    await callback.answer()
    await _apply_snooze(callback, session_factory, reminder_scheduler, time_service, int(callback.data.split(":", 1)[1]), "evening")


@router.callback_query(F.data.startswith("snooze_tomorrow_morning:"))
async def snooze_tomorrow_morning(callback: CallbackQuery, session_factory, reminder_scheduler, time_service):
    await callback.answer()
    await _apply_snooze(callback, session_factory, reminder_scheduler, time_service, int(callback.data.split(":", 1)[1]), "tomorrow_morning")


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

@router.message(Command("quick"))
async def quick(message: Message):
    await message.answer("Отправьте текст одной строкой — я подготовлю Action Preview.")


@router.message(Command("rest"))
async def rest(message: Message):
    await message.answer("Режим rest day (MVP): отправьте текстом 'завтра отдыхаю' для подтверждаемого действия.")


@router.message(Command("finance"))
@router.message(Command("workout"))
@router.message(Command("study"))
@router.message(Command("goals"))
@router.message(Command("sync_miro"))
@router.message(Command("rebuild_miro"))
@router.message(Command("archive"))
@router.message(Command("cleanup"))
@router.message(Command("settings"))
async def stubs(message: Message):
    await message.answer("Раздел в MVP в процессе: интерфейс подключен, бизнес-логика будет расширена.")
