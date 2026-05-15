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

router = Router()


@router.callback_query(F.data.startswith("confirm_preview:"))
async def confirm_preview(callback: CallbackQuery, session_factory, reminder_scheduler, time_service):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if not preview or preview.status != "pending":
            await callback.message.answer("Preview не найден или уже обработан.")
            return

        payload = json.loads(preview.preview_json)
        created_tasks_by_title = {}
        created_reminders = []

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
                created_reminders.append(reminder)

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

    for reminder in created_reminders:
        if reminder.remind_at > time_service.now():
            reminder_scheduler.schedule_reminder(reminder)

    reply_text = "Готово, задача создана."
    if created_reminders:
        if any(r.remind_at <= time_service.now() for r in created_reminders):
            reply_text = "Напоминание создано, но время уже прошло. Проверь дату/время."
        else:
            reply_text = "Готово, напоминание создано."
    elif any(i.get("type") in {"schedule_override", "rest_day"} for i in payload.get("intents", [])):
        reply_text = "Готово, день отдыха сохранён."
    await callback.message.answer(reply_text)


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
    reminder_scheduler.cancel_reminder_job(reminder_id)
    await callback.message.answer("Готово, напоминание закрыто.")


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
    reminder_scheduler.cancel_reminder_job(reminder_id)
    await callback.message.answer("Напоминание отменено.")


@router.callback_query(F.data.startswith("reminder_snooze_menu:"))
async def reminder_snooze_menu(callback: CallbackQuery):
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
            candidate = time_service.build_datetime("today", "evening")
            reminder.remind_at = candidate if candidate > now else time_service.build_datetime("tomorrow", "evening")
        else:
            reminder.remind_at = time_service.build_datetime("tomorrow", "morning")
        reminder.status = "active"
        await session.commit()
    reminder_scheduler.cancel_reminder_job(reminder_id)
    reminder_scheduler.schedule_reminder(reminder)
    await callback.message.answer(f"Перенёс на {reminder.remind_at.strftime('%H:%M')}.")


@router.callback_query(F.data.startswith("reminder_snooze_1h:"))
async def reminder_snooze_1h(callback: CallbackQuery, session_factory, reminder_scheduler, time_service):
    await callback.answer()
    await _apply_snooze(callback, session_factory, reminder_scheduler, time_service, int(callback.data.split(":", 1)[1]), "1h")


@router.callback_query(F.data.startswith("reminder_snooze_evening:"))
async def reminder_snooze_evening(callback: CallbackQuery, session_factory, reminder_scheduler, time_service):
    await callback.answer()
    await _apply_snooze(callback, session_factory, reminder_scheduler, time_service, int(callback.data.split(":", 1)[1]), "evening")


@router.callback_query(F.data.startswith("reminder_snooze_tomorrow_morning:"))
async def reminder_snooze_tomorrow_morning(callback: CallbackQuery, session_factory, reminder_scheduler, time_service):
    await callback.answer()
    await _apply_snooze(callback, session_factory, reminder_scheduler, time_service, int(callback.data.split(":", 1)[1]), "tomorrow_morning")


@router.callback_query(F.data.startswith("reminder_snooze_cancel:"))
async def reminder_snooze_cancel(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Перенос отменён.")


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


@router.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, navigation_service):
    await callback.answer()
    from bot.keyboards.main_menu import main_menu
    navigation_service.reset(callback.from_user.id)
    await callback.message.answer("813Assistant\n\nШтаб открыт.\nВыбери блок или напиши задачу обычным текстом.", reply_markup=main_menu())




@router.callback_query(F.data == "back")
async def back_callback(callback: CallbackQuery, navigation_service, session_factory, time_service):
    await callback.answer()
    screen = navigation_service.back(callback.from_user.id)
    if screen == "hq":
        from bot.handlers.menu import render_hq
        await render_hq(callback.message, session_factory, time_service)
        return
    from bot.keyboards.main_menu import main_menu
    await callback.message.answer("813Assistant\n\nШтаб открыт.\nВыбери блок или напиши задачу обычным текстом.", reply_markup=main_menu())
@router.callback_query(F.data == "next_step")
async def next_step_callback(callback: CallbackQuery, session_factory, navigation_service):
    await callback.answer()
    navigation_service.push(callback.from_user.id, "next")
    async with session_factory() as session:
        tasks = await get_active_tasks(session, callback.from_user.id)
    picks = [f"{i+1}. {t.title}" for i, t in enumerate(tasks[:3])]
    body = "\n".join(picks) if picks else "1. Закрыть один мелкий хвост.\n2. Подготовить следующий фокус."
    await callback.message.answer(f"Следующий шаг:\n\n{body}\n\nОграничение: без лишних задач.")


@router.callback_query(F.data == "add_note")
async def add_note_callback(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Принял. Напиши запись обычным текстом.")


@router.callback_query(F.data == "overload_rest")
async def overload_rest(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Принял. Экстренный отдых: 20–40 минут без телефона. Потом вернёмся к плану.")


@router.callback_query(F.data == "overload_light_plan")
async def overload_light_plan(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("СИТУАЦИЯ\nРесурс просел.\n\nВЫВОД\nРаботаем в лёгком режиме.\n\nДЕЙСТВИЕ\n1. Закрыть один обязательный пункт.\n2. Пауза 20 минут.\n3. Вернуться к следующему шагу.")


@router.callback_query(F.data == "overload_skip")
async def overload_skip(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Принял.\nРиск перегруза сохраняется.\n\nСобираю план в жёстком режиме:\n1. Закрыть критичный хвост.\n2. Сделать один учебный блок.\n3. Перепроверить ресурс через 2 часа.\n\nОграничение: без лишних задач и без добивания тела.")
