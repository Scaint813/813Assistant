from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from bot.database.models import HealthSnapshot, MemoryEntry
from bot.database.queries import create_reminder, create_task, get_pending_preview
from bot.keyboards.inline import (
    confirm_keyboard,
    training_profile_keyboard,
    training_week_keyboard,
    undo_keyboard,
    workout_exercise_keyboard,
    workout_keyboard,
    workout_move_confirm_keyboard,
    workout_move_keyboard,
    workout_rpe_keyboard,
    workout_skip_keyboard,
)
from bot.services.action_preview import render_preview

router = Router()


class EditPreviewState(StatesGroup):
    waiting_text = State()


class ReminderRescheduleState(StatesGroup):
    waiting_time = State()


class TrainingSetupState(StatesGroup):
    waiting_goal = State()
    waiting_experience = State()
    waiting_fixed = State()
    waiting_schedule = State()
    waiting_minutes = State()
    waiting_equipment = State()
    waiting_limitations = State()


class TrainingRescheduleState(StatesGroup):
    waiting_time = State()
    confirming = State()


class WorkoutLoggingState(StatesGroup):
    waiting_sets = State()


@router.callback_query(F.data.startswith("edit_preview:"))
async def edit_preview(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    await state.set_state(EditPreviewState.waiting_text)
    await state.update_data(preview_id=preview_id)
    await callback.message.edit_text(
        "Пришли исправленное поручение одним сообщением. Ничего ещё не сохранено.",
        reply_markup=None,
    )


@router.message(StateFilter(EditPreviewState.waiting_text), F.text)
async def edit_preview_text(
    message: Message,
    state: FSMContext,
    session_factory,
    intent_parser,
    screen_service,
    metric_service,
    quality_service,
):
    data = await state.get_data()
    preview_id = int(data["preview_id"])
    parsed = await intent_parser.parse_user_text(
        message.text or "",
        context={"session_factory": session_factory, "user_id": message.from_user.id},
    )
    if parsed.get("clarification"):
        await message.answer(parsed["clarification"])
        return
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, message.from_user.id)
        if not preview or preview.status != "pending":
            await message.answer("Preview уже обработан или не найден.")
            await state.clear()
            return
        await quality_service.record_feedback(
            session,
            message.from_user.id,
            preview,
            stage="preview",
            verdict="corrected",
            corrected_text=message.text or "",
        )
        preview.original_text = message.text or ""
        preview.transcript = message.text or ""
        preview.preview_json = json.dumps(parsed, ensure_ascii=False)
        await metric_service.record(
            session, message.from_user.id, "preview_edited",
            entity_type="preview", entity_id=preview.id, usage=parsed.get("usage"),
        )
        await session.commit()
    await state.clear()
    await message.answer(
        escape(render_preview(parsed)),
        reply_markup=confirm_keyboard(preview_id, parsed),
    )
    await screen_service.delete_user_input(message)


@router.callback_query(F.data.startswith("not_action_preview:"))
async def not_action_preview(
    callback: CallbackQuery,
    session_factory,
    metric_service,
    quality_service,
):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if not preview or preview.status != "pending":
            await callback.message.edit_text(
                "Это предложение уже обработано.", reply_markup=None
            )
            return
        preview.status = "not_action"
        await quality_service.record_feedback(
            session,
            callback.from_user.id,
            preview,
            stage="preview",
            verdict="not_action",
        )
        await metric_service.record(
            session,
            callback.from_user.id,
            "preview_not_action",
            entity_type="preview",
            entity_id=preview.id,
        )
        await session.commit()
    await callback.message.edit_text(
        "Понял: это не поручение. Ничего не сохранено.", reply_markup=None
    )


@router.callback_query(F.data.startswith("reminder_snooze_custom:"))
async def reminder_snooze_custom(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    reminder_id = int(callback.data.split(":", 1)[1])
    await state.set_state(ReminderRescheduleState.waiting_time)
    await state.update_data(reminder_id=reminder_id)
    await callback.message.edit_text(
        "Напиши новое время: «через 45 минут», «завтра в 10:00» или «вечером».",
        reply_markup=None,
    )


@router.message(StateFilter(ReminderRescheduleState.waiting_time), F.text)
async def reminder_snooze_custom_text(
    message: Message,
    state: FSMContext,
    reminder_scheduler,
    time_service,
    action_log_service,
    session_factory,
    miro_sync_coordinator,
    screen_service,
):
    new_time = time_service.parse_snooze_text(message.text or "")
    if new_time is None:
        await message.answer("Не понял время. Пример: «завтра в 10:00».")
        return
    data = await state.get_data()
    reminder_id = int(data["reminder_id"])
    async with session_factory() as session:
        from bot.database.queries import get_reminder_by_id

        reminder = await get_reminder_by_id(session, message.from_user.id, reminder_id)
        if not reminder:
            await state.clear()
            await message.answer("Напоминание не найдено.")
            return
        before = {"remind_at": reminder.remind_at.isoformat()}
        await action_log_service.log_update(
            session,
            message.from_user.id,
            "reminder",
            reminder.id,
            f"Перенесено напоминание: {reminder.text}",
            before,
            {"remind_at": new_time.isoformat()},
            f"reschedule:{reminder.id}:{int(time_service.now().timestamp())}",
            "telegram",
        )
        await session.commit()
    updated = await reminder_scheduler.reschedule_reminder(reminder_id, new_time)
    await state.clear()
    miro_sync_coordinator.schedule(message.from_user.id)
    await message.answer(
        f"Перенесено: {updated.text}\nНовое время: {new_time.strftime('%d.%m.%Y в %H:%M')}"
    )
    await screen_service.delete_user_input(message)


async def _undo(
    user_id: int,
    batch_key: str | None,
    session_factory,
    action_log_service,
    reminder_scheduler,
    time_service,
):
    async with session_factory() as session:
        target = batch_key or await action_log_service.latest_undoable_batch(session, user_id)
        if not target:
            return {"undone": 0, "reminder_ids": [], "reschedule_reminder_ids": [], "summaries": []}
        result = await action_log_service.undo_batch(session, user_id, target, time_service.now())
        await session.commit()
    for reminder_id in result["reminder_ids"]:
        reminder_scheduler.cancel_reminder_job(reminder_id)
    for reminder_id in result.get("reschedule_reminder_ids", []):
        async with session_factory() as session:
            from bot.database.queries import get_reminder_by_id
            reminder = await get_reminder_by_id(session, user_id, reminder_id)
        if reminder and reminder.status == "active" and reminder.remind_at > time_service.now():
            reminder_scheduler.schedule_reminder(reminder)
    return result


@router.callback_query(F.data.startswith("result_ok:"))
async def result_ok(
    callback: CallbackQuery,
    session_factory,
    metric_service,
    quality_service,
):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if preview:
            await quality_service.record_feedback(
                session,
                callback.from_user.id,
                preview,
                stage="result",
                verdict="correct",
            )
            await metric_service.record(
                session,
                callback.from_user.id,
                "result_confirmed",
                entity_type="preview",
                entity_id=preview.id,
            )
            await session.commit()
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("result_fix:"))
async def result_fix(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory,
    action_log_service,
    reminder_scheduler,
    time_service,
    miro_sync_coordinator,
    metric_service,
    quality_service,
):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    batch_key = f"preview:{preview_id}"
    result = await _undo(
        callback.from_user.id,
        batch_key,
        session_factory,
        action_log_service,
        reminder_scheduler,
        time_service,
    )
    if not result["undone"]:
        await callback.message.edit_text(
            "Не смог безопасно отменить сохранённое. Используй «Изменить» у исходного preview.",
            reply_markup=None,
        )
        return
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if not preview:
            await callback.message.edit_text("Исходное поручение не найдено.", reply_markup=None)
            return
        preview.status = "pending"
        await quality_service.record_feedback(
            session,
            callback.from_user.id,
            preview,
            stage="result",
            verdict="correction_requested",
            metadata={"undone_actions": result["undone"]},
        )
        await metric_service.record(
            session,
            callback.from_user.id,
            "result_correction_requested",
            entity_type="preview",
            entity_id=preview.id,
            metadata={"undone_actions": result["undone"]},
        )
        await session.commit()
    await state.set_state(EditPreviewState.waiting_text)
    await state.update_data(preview_id=preview_id)
    miro_sync_coordinator.schedule(callback.from_user.id)
    await callback.message.edit_text(
        "Сохранённое отменено. Пришли исправленное поручение одним сообщением.",
        reply_markup=None,
    )


@router.callback_query(F.data.startswith("undo_batch:"))
async def undo_batch(
    callback: CallbackQuery,
    session_factory,
    action_log_service,
    reminder_scheduler,
    time_service,
    miro_sync_coordinator,
):
    await callback.answer()
    batch_key = callback.data.split(":", 1)[1]
    result = await _undo(
        callback.from_user.id,
        batch_key,
        session_factory,
        action_log_service,
        reminder_scheduler,
        time_service,
    )
    miro_sync_coordinator.schedule(callback.from_user.id)
    text = (
        f"Отменено действий: {result['undone']}."
        if result["undone"]
        else "Это действие уже отменено или недоступно для отмены."
    )
    await callback.message.edit_text(text, reply_markup=None)


@router.message(Command("undo"))
async def undo_last(
    message: Message,
    session_factory,
    action_log_service,
    reminder_scheduler,
    time_service,
    miro_sync_coordinator,
    screen_service,
):
    result = await _undo(
        message.from_user.id,
        None,
        session_factory,
        action_log_service,
        reminder_scheduler,
        time_service,
    )
    miro_sync_coordinator.schedule(message.from_user.id)
    await message.answer(
        f"Отменено действий: {result['undone']}."
        if result["undone"]
        else "Нет действий, которые можно отменить."
    )
    await screen_service.delete_user_input(message)


@router.message(Command("activity"))
async def activity(message: Message, session_factory, action_log_service, screen_service):
    async with session_factory() as session:
        rows = await action_log_service.recent(session, message.from_user.id, 20)
    if not rows:
        text = "Журнал действий пуст."
    else:
        lines = ["Последние действия:"]
        for row in rows:
            icon = "↩" if row.status == "undone" else "✓" if row.status == "applied" else "!"
            lines.append(f"{icon} {row.created_at.strftime('%d.%m %H:%M')} · {row.summary}")
        lines.append("\n/undo — отменить последний пакет создания.")
        text = "\n".join(lines)
    await message.answer(text)
    await screen_service.delete_user_input(message)


@router.message(Command("remember"))
async def remember(message: Message, session_factory, screen_service):
    raw = (message.text or "").partition(" ")[2].strip()
    if "=" not in raw:
        await message.answer("Формат: /remember ключ=значение")
        return
    key, value = (part.strip() for part in raw.split("=", 1))
    if not key or not value:
        await message.answer("Нужны и ключ, и значение.")
        return
    async with session_factory() as session:
        result = await session.execute(
            select(MemoryEntry).where(
                MemoryEntry.user_id == message.from_user.id,
                MemoryEntry.key == key[:128],
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = MemoryEntry(user_id=message.from_user.id, key=key[:128], value=value[:2000])
            session.add(row)
        else:
            row.value = value[:2000]
        await session.commit()
    await message.answer(f"Запомнил: {key} = {value}")
    await screen_service.delete_user_input(message)


@router.message(Command("memory"))
async def memory(message: Message, session_factory, screen_service):
    async with session_factory() as session:
        result = await session.execute(
            select(MemoryEntry)
            .where(MemoryEntry.user_id == message.from_user.id)
            .order_by(MemoryEntry.key.asc())
        )
        rows = list(result.scalars().all())
    text = "Память пуста." if not rows else "Что я помню:\n\n" + "\n".join(
        f"• {row.key}: {row.value} [{row.source}]" for row in rows
    )
    await message.answer(text + "\n\nУдалить: /forget ключ")
    await screen_service.delete_user_input(message)


@router.message(Command("forget"))
async def forget(message: Message, session_factory, screen_service):
    key = (message.text or "").partition(" ")[2].strip()
    async with session_factory() as session:
        result = await session.execute(
            select(MemoryEntry).where(
                MemoryEntry.user_id == message.from_user.id,
                MemoryEntry.key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            await session.delete(row)
            await session.commit()
    await message.answer("Удалено." if row else "Такой записи нет.")
    await screen_service.delete_user_input(message)


@router.message(Command("steps"))
async def steps(message: Message, session_factory, health_service, screen_service):
    async with session_factory() as session:
        snapshot = await health_service.latest_snapshot(session, message.from_user.id)
    if not snapshot:
        text = "Данных о шагах пока нет. Настрой Health Shortcut bridge."
    else:
        gap = max(0, snapshot.step_goal - snapshot.steps)
        text = (
            f"Health · {snapshot.date}\n\n"
            f"Шаги: {snapshot.steps} / {snapshot.step_goal}\n"
            f"Осталось: {gap}\n"
            f"Тренировка: {snapshot.workout_minutes} мин\n"
            f"Сон: {snapshot.sleep_minutes // 60} ч {snapshot.sleep_minutes % 60} мин"
        )
        if snapshot.resting_heart_rate:
            text += f"\nПульс покоя: {snapshot.resting_heart_rate} уд/мин"
        if snapshot.hrv_ms:
            text += f"\nHRV: {snapshot.hrv_ms} мс"
    await message.answer(text)
    await screen_service.delete_user_input(message)


async def _snapshot(session, user_id: int, snapshot_id: int):
    result = await session.execute(
        select(HealthSnapshot).where(
            HealthSnapshot.id == snapshot_id,
            HealthSnapshot.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


@router.callback_query(F.data.startswith("health_walk_schedule:"))
async def health_walk_schedule(
    callback: CallbackQuery,
    session_factory,
    health_service,
    action_log_service,
    reminder_scheduler,
    task_prioritization_service,
    time_service,
    miro_sync_coordinator,
):
    await callback.answer()
    snapshot_id = int(callback.data.split(":", 1)[1])
    batch_key = f"health:{snapshot_id}:{int(time_service.now().timestamp())}"
    async with session_factory() as session:
        snapshot = await _snapshot(session, callback.from_user.id, snapshot_id)
        if not snapshot:
            await callback.message.edit_text("Health snapshot не найден.", reply_markup=None)
            return
        gap = max(0, snapshot.step_goal - snapshot.steps)
        minutes = max(10, round(gap / 105 / 5) * 5)
        task = await create_task(
            session,
            callback.from_user.id,
            f"Прогулка: добрать примерно {gap} шагов",
            outcome=f"Дневная норма {snapshot.step_goal} шагов",
            next_action=f"Выйти на прогулку на {minutes} минут",
            importance=3,
            urgency=4,
            estimated_minutes=minutes,
            duration_confirmed=True,
            deadline_confirmed=True,
            planning_state="ready",
            category="health",
            priority="medium",
        )
        remind_at = time_service.now() + timedelta(minutes=30)
        reminder = await create_reminder(
            session,
            callback.from_user.id,
            f"Прогулка на {minutes} минут — осталось около {gap} шагов",
            remind_at,
            related_entity_type="task",
            related_entity_id=task.id,
            recurrence="none",
        )
        await action_log_service.log_create(
            session, callback.from_user.id, "task", task.id,
            f"Запланирована прогулка: {minutes} минут", batch_key, "health",
        )
        await action_log_service.log_create(
            session, callback.from_user.id, "reminder", reminder.id,
            "Создано напоминание о прогулке", batch_key, "health",
        )
        await health_service.mark_nudge(session, callback.from_user.id, snapshot_id, "scheduled")
        await task_prioritization_service.refresh(session, callback.from_user.id, time_service.now())
        await session.commit()
    reminder_scheduler.schedule_reminder(reminder)
    miro_sync_coordinator.schedule(callback.from_user.id)
    await callback.message.edit_text(
        f"Прогулка запланирована на {remind_at.strftime('%H:%M')}.",
        reply_markup=undo_keyboard(batch_key),
    )


@router.callback_query(F.data.startswith("health_walk_later:"))
async def health_walk_later(
    callback: CallbackQuery,
    session_factory,
    health_service,
    reminder_scheduler,
    time_service,
):
    await callback.answer()
    snapshot_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        snapshot = await _snapshot(session, callback.from_user.id, snapshot_id)
        if not snapshot:
            return
        reminder = await create_reminder(
            session,
            callback.from_user.id,
            "Проверить остаток шагов и решить насчёт прогулки",
            time_service.now() + timedelta(hours=1),
            recurrence="none",
        )
        await health_service.mark_nudge(session, callback.from_user.id, snapshot_id, "later")
        await session.commit()
    reminder_scheduler.schedule_reminder(reminder)
    await callback.message.edit_text("Напомню через час.", reply_markup=None)


@router.callback_query(F.data.startswith("health_walk_skip:"))
async def health_walk_skip(callback: CallbackQuery, session_factory, health_service):
    await callback.answer()
    snapshot_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        await health_service.mark_nudge(session, callback.from_user.id, snapshot_id, "skipped")
        await session.commit()
    await callback.message.edit_text("Сегодня пропускаем. Больше не напомню.", reply_markup=None)


TRAINING_TEMPLATE = (
    "Можно заполнить профиль одной строкой:\n\n"
    "цель=стать сильнее; уровень=новичок; "
    "фиксированные=бокс вт 19:00 тяжёлая; дни=пн 19:00, пт 19:00; "
    "минуты=45; оборудование=гантели; ограничения=нет"
)
TRAINING_DAY_LABELS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


@router.message(Command("training_setup"))
async def training_setup(
    message: Message,
    state: FSMContext,
    training_service,
    session_factory,
    screen_service,
    bot,
):
    raw = (message.text or "").partition(" ")[2].strip()
    if raw:
        await _save_training_profile(
            message,
            raw,
            state,
            training_service,
            session_factory,
            screen_service,
            bot,
        )
        return
    await state.clear()
    await _training_prompt(
        message,
        state,
        TrainingSetupState.waiting_goal,
        session_factory,
        screen_service,
        bot,
        "Настроим тренировки · шаг 1 из 7\n\n"
        "Какая главная цель и что хочется развивать?\n\n"
        "Например: «стать сильнее, приоритет — спина и ноги» или "
        "«поддерживать форму и выносливость».",
    )


@router.message(StateFilter(TrainingSetupState.waiting_goal), F.text)
async def training_setup_goal(message, state, session_factory, screen_service, bot):
    await state.update_data(goal=(message.text or "").strip())
    await _training_prompt(
        message,
        state,
        TrainingSetupState.waiting_experience,
        session_factory,
        screen_service,
        bot,
        "Настроим тренировки · шаг 2 из 7\n\n"
        "Какой у тебя опыт?\n\nНовичок / занимаюсь регулярно / продвинутый.",
    )


@router.message(StateFilter(TrainingSetupState.waiting_experience), F.text)
async def training_setup_experience(message, state, session_factory, screen_service, bot):
    await state.update_data(experience=(message.text or "").strip())
    await _training_prompt(
        message,
        state,
        TrainingSetupState.waiting_fixed,
        session_factory,
        screen_service,
        bot,
        "Настроим тренировки · шаг 3 из 7\n\n"
        "Какие занятия уже стоят фиксированно?\n\n"
        "Например: «бокс вт 19:00 тяжёлая; футбол сб 12:00». "
        "Если таких нет — напиши «нет».",
    )


@router.message(StateFilter(TrainingSetupState.waiting_fixed), F.text)
async def training_setup_fixed(
    message,
    state,
    training_service,
    session_factory,
    screen_service,
    bot,
):
    fixed = (message.text or "").strip()
    try:
        training_service.parse_fixed_sessions(fixed)
    except ValueError as exc:
        await _training_prompt(
            message,
            state,
            TrainingSetupState.waiting_fixed,
            session_factory,
            screen_service,
            bot,
            f"Не получилось разобрать фиксированные занятия: {exc}.\n\n"
            "Пример: «бокс вт 19:00 тяжёлая; футбол сб 12:00».",
        )
        return
    await state.update_data(fixed=fixed)
    await _training_prompt(
        message,
        state,
        TrainingSetupState.waiting_schedule,
        session_factory,
        screen_service,
        bot,
        "Настроим тренировки · шаг 4 из 7\n\n"
        "В какие дни поставить дополнительные тренировки, которые составит ассистент?\n\n"
        "Например: «пн 19:00, пт 18:30». Если дополнительные не нужны — «нет».",
    )


@router.message(StateFilter(TrainingSetupState.waiting_schedule), F.text)
async def training_setup_schedule(
    message,
    state,
    training_service,
    session_factory,
    screen_service,
    bot,
):
    schedule = (message.text or "").strip()
    try:
        training_service.parse_schedule(schedule)
    except ValueError as exc:
        await _training_prompt(
            message,
            state,
            TrainingSetupState.waiting_schedule,
            session_factory,
            screen_service,
            bot,
            f"Не получилось разобрать расписание: {exc}.\n\n"
            "Напиши, например: «пн 19:00, ср 18:30, сб 12:00».",
        )
        return
    await state.update_data(schedule=schedule)
    await _training_prompt(
        message,
        state,
        TrainingSetupState.waiting_minutes,
        session_factory,
        screen_service,
        bot,
        "Настроим тренировки · шаг 5 из 7\n\n"
        "Сколько минут реально есть на одно занятие?\n\nНапример: 30, 45 или 60.",
    )


@router.message(StateFilter(TrainingSetupState.waiting_minutes), F.text)
async def training_setup_minutes(message, state, session_factory, screen_service, bot):
    match = re.search(r"\d+", message.text or "")
    if not match or not 15 <= int(match.group()) <= 120:
        await _training_prompt(
            message,
            state,
            TrainingSetupState.waiting_minutes,
            session_factory,
            screen_service,
            bot,
            "Нужна длительность от 15 до 120 минут. Например: «45 минут».",
        )
        return
    await state.update_data(minutes=match.group())
    await _training_prompt(
        message,
        state,
        TrainingSetupState.waiting_equipment,
        session_factory,
        screen_service,
        bot,
        "Настроим тренировки · шаг 6 из 7\n\n"
        "Где занимаешься и какое есть оборудование?\n\n"
        "Например: «зал», «дома, гантели и резинки» или «без оборудования».",
    )


@router.message(StateFilter(TrainingSetupState.waiting_equipment), F.text)
async def training_setup_equipment(message, state, session_factory, screen_service, bot):
    await state.update_data(equipment=(message.text or "").strip())
    await _training_prompt(
        message,
        state,
        TrainingSetupState.waiting_limitations,
        session_factory,
        screen_service,
        bot,
        "Настроим тренировки · шаг 7 из 7\n\n"
        "Есть ли ограничения, боль, травмы или запреты врача?\n\n"
        "Если нет — просто напиши «нет».",
    )


@router.message(StateFilter(TrainingSetupState.waiting_limitations), F.text)
async def training_setup_limitations(
    message,
    state,
    training_service,
    session_factory,
    screen_service,
    bot,
):
    data = await state.get_data()
    goal = data.get("goal", "общая физическая форма")
    raw = (
        f"цель={goal}; фокус={goal}; уровень={data.get('experience', 'новичок')}; "
        f"фиксированные={data.get('fixed', 'нет')}; "
        f"дни={data.get('schedule', '')}; минуты={data.get('minutes', '45')}; "
        f"оборудование={data.get('equipment', 'без оборудования')}; "
        f"ограничения={(message.text or '').strip()}"
    )
    await _save_training_profile(
        message,
        raw,
        state,
        training_service,
        session_factory,
        screen_service,
        bot,
    )


async def _training_prompt(
    message,
    state,
    next_state,
    session_factory,
    screen_service,
    bot,
    text,
):
    await state.set_state(next_state)
    await _render_training_screen(
        message,
        session_factory,
        screen_service,
        bot,
        text,
    )


async def _render_training_screen(
    message,
    session_factory,
    screen_service,
    bot,
    text,
    reply_markup=None,
):
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot,
            session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text,
            reply_markup=reply_markup,
        )
        await session.commit()
    await screen_service.delete_user_input(message)


async def _save_training_profile(
    message,
    raw,
    state,
    training_service,
    session_factory,
    screen_service,
    bot,
):
    try:
        async with session_factory() as session:
            profile = await training_service.save_profile(session, message.from_user.id, raw)
            await session.commit()
    except (ValueError, AttributeError) as exc:
        await _render_training_screen(
            message,
            session_factory,
            screen_service,
            bot,
            f"Не удалось сохранить профиль: {exc}\n\n{TRAINING_TEMPLATE}",
        )
        return
    await state.clear()
    schedule = json.loads(profile.preferred_times_json or "{}")
    days = json.loads(profile.preferred_days_json or "[]")
    fixed = json.loads(profile.fixed_sessions_json or "[]")
    clock_note = ", ".join(
        f"{TRAINING_DAY_LABELS[int(day)]} {schedule.get(str(day), '18:30')}"
        for day in days
    ) or "дополнительных нет"
    await _render_training_screen(
        message,
        session_factory,
        screen_service,
        bot,
        f"Тренировочный профиль сохранён.\n\nЦель: {escape(profile.goal)}\n"
        f"Занятий: {profile.days_per_week} в неделю по {profile.session_minutes} минут\n"
        f"Дополнительные: {clock_note}\n"
        f"Фиксированные: {len(fixed)}",
        reply_markup=training_profile_keyboard(),
    )


def _format_training_plan(plan, sessions, selected=None) -> str:
    data = json.loads(plan.plan_json)
    lines = ["Тренировочная неделя", plan.adjustment_reason, ""]
    status_labels = {
        "planned": "запланировано",
        "completed": "выполнено",
        "skipped": "пропущено",
        "in_progress": "идёт сейчас",
    }
    for item in sorted(sessions, key=lambda workout: workout.scheduled_for):
        lines.append(
            f"{TRAINING_DAY_LABELS[item.scheduled_for.weekday()]} "
            f"{item.scheduled_for.strftime('%d.%m %H:%M')} · "
            f"{escape(item.title)} · {status_labels.get(item.status, item.status)}"
        )
    if selected is not None:
        details = json.loads(selected.details_json or "{}")
        activity_labels = {
            "strength": "силовая",
            "combat": "единоборства",
            "team_sport": "командный спорт",
            "running": "бег",
            "swimming": "плавание",
            "mobility": "мобилити",
            "cycling": "велотренировка",
            "other": "другое занятие",
        }
        load_labels = {"light": "лёгкая", "medium": "средняя", "high": "тяжёлая"}
        meta = [
            activity_labels.get(selected.activity_type, selected.activity_type),
            load_labels.get(selected.load_level, selected.load_level),
        ]
        if selected.is_fixed:
            meta.append("фиксировано")
        lines += [
            "",
            f"Выбрано: {escape(selected.title)}",
            f"{selected.scheduled_for.strftime('%d.%m в %H:%M')} · {' · '.join(meta)}",
        ]
        if selected.status in {"completed", "skipped"}:
            result = status_labels[selected.status]
            if selected.rpe:
                result += f" · RPE {selected.rpe}/10"
            lines.append(result)
        else:
            lines.append(
                f"Фокус: {escape(str(details.get('focus', 'общая физическая форма')))}; "
                f"около {details.get('estimated_minutes', 45)} мин"
            )
            if not selected.is_fixed:
                lines.append(f"Разминка: {details.get('warmup_minutes', 5)} мин")
                for exercise in details.get("exercises", []):
                    line = (
                        f"• {escape(str(exercise['exercise']))}: "
                        f"{exercise['sets']}×{exercise['reps']}"
                    )
                    if exercise.get("suggested_weight_kg"):
                        line += f" · ориентир {exercise['suggested_weight_kg']} кг"
                    lines.append(line)
                    if exercise.get("progression_note"):
                        lines.append(f"  {escape(str(exercise['progression_note']))}")
                lines.append(f"Заминка: {details.get('cooldown_minutes', 5)} мин")
    lines += ["", data["safety"], "", "Выбери день или действие кнопками ниже."]
    return "\n".join(lines)


def _training_week_markup(training_service, sessions, selected):
    status_icons = {
        "planned": "•",
        "in_progress": "▶",
        "completed": "✓",
        "skipped": "–",
    }
    items = [
        (
            item.id,
            f"{status_icons.get(item.status, '•')} "
            f"{TRAINING_DAY_LABELS[item.scheduled_for.weekday()]} "
            f"{item.scheduled_for.strftime('%d.%m')} · {item.title[:28]}",
        )
        for item in sorted(sessions, key=lambda workout: workout.scheduled_for)
    ]
    actions = _workout_action_keyboard(training_service, selected) if selected else None
    return training_week_keyboard(items, selected.id if selected else None, actions)


def _format_current_exercise(training_service, workout) -> str:
    details = json.loads(workout.details_json or "{}")
    exercises = list(details.get("exercises") or [])
    exercise = training_service.current_exercise(workout)
    if exercise is None:
        return (
            f"{escape(workout.title)}\n\n"
            "Все упражнения пройдены. Заверши тренировку и оцени нагрузку."
        )
    position = (workout.current_exercise_index or 0) + 1
    guidance = ""
    if exercise.get("last_result"):
        guidance += f"\nПоследний результат: {escape(str(exercise['last_result']))}"
    if exercise.get("suggested_weight_kg"):
        guidance += f"\nОриентир веса: {exercise['suggested_weight_kg']} кг"
    if exercise.get("progression_note"):
        guidance += f"\n{escape(str(exercise['progression_note']))}"
    return (
        f"Тренировка идёт · упражнение {position} из {len(exercises)}\n\n"
        f"{escape(str(exercise.get('exercise', 'Упражнение')))}\n"
        f"План: {exercise.get('sets', 3)}×{exercise.get('reps', '8–12')} · "
        f"RPE {exercise.get('target_rpe', '6–8')}\n"
        f"Отдых: {exercise.get('rest_seconds', 90)} сек"
        f"{guidance}\n\n"
        "После упражнения запиши фактические подходы одной строкой."
    )


def _workout_action_keyboard(training_service, workout):
    if workout.status not in {"planned", "in_progress"}:
        return None
    if workout.status != "in_progress":
        return workout_keyboard(workout.id)
    if training_service.current_exercise(workout) is None:
        return workout_rpe_keyboard(workout.id)
    return workout_exercise_keyboard(workout.id)


@router.message(Command("training_plan"))
async def training_plan(
    message: Message,
    session_factory,
    training_service,
    reminder_scheduler,
    time_service,
    miro_sync_coordinator,
    screen_service,
    bot,
):
    try:
        async with session_factory() as session:
            plan, sessions, reminders = await training_service.generate_week(
                session, message.from_user.id, time_service.now()
            )
            await session.commit()
    except ValueError as exc:
        await _render_training_screen(
            message, session_factory, screen_service, bot, str(exc)
        )
        return
    for reminder in reminders:
        reminder_scheduler.schedule_reminder(reminder)
    active = sorted(
        (item for item in sessions if item.status in {"planned", "in_progress"}),
        key=lambda item: (item.status != "in_progress", item.scheduled_for),
    )
    selected = active[0] if active else sessions[-1] if sessions else None
    await _render_training_screen(
        message,
        session_factory,
        screen_service,
        bot,
        _format_training_plan(plan, sessions, selected),
        reply_markup=_training_week_markup(training_service, sessions, selected),
    )
    miro_sync_coordinator.schedule(message.from_user.id)


@router.callback_query(F.data == "training_generate_plan")
async def training_generate_plan(
    callback: CallbackQuery,
    session_factory,
    training_service,
    reminder_scheduler,
    time_service,
    miro_sync_coordinator,
):
    await callback.answer("Составляю неделю...")
    try:
        async with session_factory() as session:
            plan, sessions, reminders = await training_service.generate_week(
                session, callback.from_user.id, time_service.now()
            )
            await session.commit()
    except ValueError as exc:
        await callback.message.edit_text(str(exc), reply_markup=None)
        return
    for reminder in reminders:
        reminder_scheduler.schedule_reminder(reminder)
    active = sorted(
        (item for item in sessions if item.status in {"planned", "in_progress"}),
        key=lambda item: (item.status != "in_progress", item.scheduled_for),
    )
    selected = active[0] if active else sessions[-1] if sessions else None
    await callback.message.edit_text(
        _format_training_plan(plan, sessions, selected),
        reply_markup=_training_week_markup(training_service, sessions, selected),
    )
    miro_sync_coordinator.schedule(callback.from_user.id)


@router.callback_query(F.data.startswith("training_workout:"))
async def training_workout_view(
    callback: CallbackQuery,
    session_factory,
    training_service,
):
    await callback.answer()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    try:
        async with session_factory() as session:
            plan, sessions, selected = await training_service.get_workout_context(
                session,
                callback.from_user.id,
                workout_id,
            )
    except ValueError as exc:
        await callback.message.edit_text(str(exc), reply_markup=None)
        return
    await callback.message.edit_text(
        _format_training_plan(plan, sessions, selected),
        reply_markup=_training_week_markup(training_service, sessions, selected),
    )


@router.message(
    F.text.regexp(
        r"(?i)(состав\w*|созда\w*|сдела\w*|распиш\w*|покаж\w*|открой\w*)"
        r".{0,25}(план трениров|тренировочн\w* план|тренировки на)"
    )
)
async def training_plan_text(
    message,
    state,
    session_factory,
    training_service,
    reminder_scheduler,
    time_service,
    miro_sync_coordinator,
    screen_service,
    bot,
):
    async with session_factory() as session:
        profile = await training_service.get_profile(session, message.from_user.id)
    if profile is None:
        await state.clear()
        await _training_prompt(
            message,
            state,
            TrainingSetupState.waiting_goal,
            session_factory,
            screen_service,
            bot,
            "Сначала соберу базовые вводные · шаг 1 из 7\n\n"
            "Какая главная цель и что хочется развивать?\n\n"
            "Например: «стать сильнее, приоритет — спина и ноги».",
        )
        return
    await training_plan(
        message,
        session_factory,
        training_service,
        reminder_scheduler,
        time_service,
        miro_sync_coordinator,
        screen_service,
        bot,
    )


@router.message(Command("workout_done"))
async def workout_done(
    message,
    session_factory,
    training_service,
    time_service,
    screen_service,
    bot,
):
    raw = (message.text or "").partition(" ")[2].strip()
    if not raw:
        await _render_training_screen(
            message,
            session_factory,
            screen_service,
            bot,
            "Укажи субъективную нагрузку от 1 до 10.\n\n"
            "Например: /workout_done 7 нормальная тренировка",
        )
        return
    first, _, notes = raw.partition(" ")
    try:
        rpe = int(first)
        async with session_factory() as session:
            workout = await training_service.complete_nearest(
                session, message.from_user.id, time_service.now(), rpe, notes
            )
            await session.commit()
    except ValueError as exc:
        await _render_training_screen(
            message, session_factory, screen_service, bot, str(exc)
        )
        return
    await _render_training_screen(
        message,
        session_factory,
        screen_service,
        bot,
        f"Тренировка отмечена выполненной. RPE {workout.rpe}/10. "
        "Следующий недельный план учтёт эту нагрузку.",
    )


@router.callback_query(F.data.startswith("workout_actions:"))
async def workout_actions(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory,
    training_service,
):
    await callback.answer()
    await state.clear()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    try:
        async with session_factory() as session:
            plan, sessions, workout = await training_service.get_workout_context(
                session,
                callback.from_user.id,
                workout_id,
            )
    except ValueError as exc:
        await callback.message.edit_text(str(exc), reply_markup=None)
        return
    await callback.message.edit_text(
        _format_training_plan(plan, sessions, workout),
        reply_markup=_training_week_markup(training_service, sessions, workout),
    )


@router.callback_query(F.data.startswith("workout_start:"))
async def workout_start(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory,
    training_service,
    time_service,
    reminder_scheduler,
):
    await callback.answer("Тренировка началась")
    await state.clear()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    try:
        async with session_factory() as session:
            workout, reminder = await training_service.start_workout(
                session,
                callback.from_user.id,
                workout_id,
                time_service.now(),
            )
            await session.commit()
    except ValueError as exc:
        await callback.message.edit_text(str(exc), reply_markup=None)
        return
    if reminder:
        reminder_scheduler.cancel_reminder_job(reminder.id)
    if training_service.current_exercise(workout) is None:
        text = (
            f"Тренировка «{escape(workout.title)}» началась.\n\n"
            "Для фиксированного занятия упражнения не расписываются. "
            "Когда закончишь, оцени общую нагрузку."
        )
    else:
        text = _format_current_exercise(training_service, workout)
    await callback.message.edit_text(
        text,
        reply_markup=_workout_action_keyboard(training_service, workout),
    )


@router.callback_query(F.data.startswith("workout_log_sets:"))
async def workout_log_sets(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory,
    training_service,
):
    await callback.answer()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    try:
        async with session_factory() as session:
            workout = await training_service._workout_with_status(
                session,
                callback.from_user.id,
                workout_id,
                {"in_progress"},
            )
            exercise = training_service.current_exercise(workout)
    except ValueError as exc:
        await callback.message.edit_text(str(exc), reply_markup=None)
        return
    if exercise is None:
        await callback.message.edit_text(
            "Все упражнения уже пройдены. Оцени итоговую нагрузку.",
            reply_markup=workout_rpe_keyboard(workout_id),
        )
        return
    await state.set_state(WorkoutLoggingState.waiting_sets)
    await state.update_data(workout_id=workout_id)
    await callback.message.edit_text(
        f"{escape(str(exercise.get('exercise', 'Упражнение')))}\n\n"
        "Напиши фактические подходы:\n"
        "• с весом: 10x40, 10x40, 8x40\n"
        "• без веса: 12, 12, 10\n\n"
        "Сначала повторения, затем вес в кг.",
        reply_markup=workout_exercise_keyboard(workout_id),
    )


@router.message(StateFilter(WorkoutLoggingState.waiting_sets), F.text)
async def workout_log_sets_text(
    message: Message,
    state: FSMContext,
    session_factory,
    training_service,
    time_service,
    screen_service,
    bot,
):
    data = await state.get_data()
    workout_id = int(data["workout_id"])
    try:
        entries = training_service.parse_set_entries(message.text or "")
        async with session_factory() as session:
            workout, next_exercise = await training_service.record_current_exercise(
                session,
                message.from_user.id,
                workout_id,
                entries,
                time_service.now(),
            )
            await session.commit()
    except ValueError as exc:
        await _render_training_screen(
            message,
            session_factory,
            screen_service,
            bot,
            f"{exc}.\n\nПопробуй ещё раз, например: 10x40, 10x40, 8x40",
        )
        return
    await state.clear()
    if next_exercise is None:
        text = (
            f"Подходы сохранены: {len(entries)}.\n\n"
            "Все упражнения пройдены. Какой была общая нагрузка?"
        )
    else:
        text = f"Подходы сохранены: {len(entries)}.\n\n{_format_current_exercise(training_service, workout)}"
    await _render_training_screen(
        message,
        session_factory,
        screen_service,
        bot,
        text,
        reply_markup=_workout_action_keyboard(training_service, workout),
    )


@router.callback_query(F.data.startswith("workout_skip_exercise:"))
async def workout_skip_exercise(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory,
    training_service,
):
    await callback.answer("Упражнение пропущено")
    await state.clear()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    try:
        async with session_factory() as session:
            workout, next_exercise = await training_service.skip_current_exercise(
                session,
                callback.from_user.id,
                workout_id,
            )
            await session.commit()
    except ValueError as exc:
        await callback.message.edit_text(str(exc), reply_markup=None)
        return
    text = (
        _format_current_exercise(training_service, workout)
        if next_exercise is not None
        else "Все упражнения пройдены. Какой была общая нагрузка?"
    )
    await callback.message.edit_text(
        text,
        reply_markup=_workout_action_keyboard(training_service, workout),
    )


@router.callback_query(F.data.startswith("workout_move_menu:"))
async def workout_move_menu(callback: CallbackQuery):
    await callback.answer()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    await callback.message.edit_reply_markup(reply_markup=workout_move_keyboard(workout_id))


@router.callback_query(F.data.startswith("workout_move_days:"))
async def workout_move_days(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory,
    training_service,
    time_service,
):
    await callback.answer()
    _, workout_id_raw, days_raw = callback.data.split(":")
    workout_id = int(workout_id_raw)
    async with session_factory() as session:
        workout = await training_service._planned_workout(
            session, callback.from_user.id, workout_id
        )
        current = workout.scheduled_for
        new_time = current + timedelta(days=int(days_raw))
        conflicts = await training_service.analyze_reschedule(
            session,
            callback.from_user.id,
            workout_id,
            new_time,
            time_service.now(),
        )
    await state.update_data(workout_id=workout.id, new_time=new_time.isoformat())
    await state.set_state(TrainingRescheduleState.confirming)
    await callback.message.edit_text(
        _format_reschedule_preview(workout, new_time, conflicts),
        reply_markup=workout_move_confirm_keyboard(
            workout.id,
            has_conflicts=bool(conflicts),
        ),
    )


@router.callback_query(F.data.startswith("workout_move_custom:"))
async def workout_move_custom(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(workout_id=workout_id)
    await state.set_state(TrainingRescheduleState.waiting_time)
    await callback.message.edit_text(
        "Когда перенести тренировку?\n\n"
        "Например: «завтра в 19:00», «в пятницу в 18:30» или «через 2 часа».",
        reply_markup=None,
    )


@router.message(StateFilter(TrainingRescheduleState.waiting_time), F.text)
async def workout_move_custom_time(
    message,
    state,
    session_factory,
    training_service,
    time_service,
    screen_service,
    bot,
):
    data = await state.get_data()
    workout_id = int(data["workout_id"])
    async with session_factory() as session:
        workout = await training_service._planned_workout(
            session, message.from_user.id, workout_id
        )
    new_time = _workout_target_from_text(
        message.text or "", workout.scheduled_for, time_service
    )
    if new_time is None or new_time <= time_service.now():
        await _training_prompt(
            message,
            state,
            TrainingRescheduleState.waiting_time,
            session_factory,
            screen_service,
            bot,
            "Не понял новое время. Напиши, например: «завтра в 19:00».",
        )
        return
    async with session_factory() as session:
        conflicts = await training_service.analyze_reschedule(
            session,
            message.from_user.id,
            workout.id,
            new_time,
            time_service.now(),
        )
    await state.update_data(new_time=new_time.isoformat())
    await state.set_state(TrainingRescheduleState.confirming)
    await _render_training_screen(
        message,
        session_factory,
        screen_service,
        bot,
        _format_reschedule_preview(workout, new_time, conflicts),
        reply_markup=workout_move_confirm_keyboard(
            workout.id,
            has_conflicts=bool(conflicts),
        ),
    )


@router.callback_query(F.data.startswith("workout_move_confirm:"))
async def workout_move_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory,
    training_service,
    time_service,
    reminder_scheduler,
):
    await callback.answer()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    data = await state.get_data()
    new_time_raw = data.get("new_time")
    if str(data.get("workout_id", "")) != str(workout_id) or not new_time_raw:
        await callback.message.edit_text(
            "Срок подтверждения переноса истёк. Открой /training_plan и попробуй ещё раз.",
            reply_markup=None,
        )
        await state.clear()
        return
    new_time = datetime.fromisoformat(new_time_raw)
    async with session_factory() as session:
        workout, reminder = await training_service.reschedule_workout(
            session,
            callback.from_user.id,
            workout_id,
            new_time,
            time_service.now(),
        )
        plan, sessions, workout = await training_service.get_workout_context(
            session,
            callback.from_user.id,
            workout.id,
        )
        await session.commit()
    await state.clear()
    if reminder:
        reminder_scheduler.cancel_reminder_job(reminder.id)
        reminder_scheduler.schedule_reminder(reminder)
    await callback.message.edit_text(
        "Перенесена только выбранная тренировка.\n\n"
        + _format_training_plan(plan, sessions, workout),
        reply_markup=_training_week_markup(training_service, sessions, workout),
    )


@router.callback_query(F.data.startswith("workout_move_replan:"))
async def workout_move_replan(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory,
    training_service,
    time_service,
    reminder_scheduler,
):
    await callback.answer()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    data = await state.get_data()
    new_time_raw = data.get("new_time")
    if str(data.get("workout_id", "")) != str(workout_id) or not new_time_raw:
        await callback.message.edit_text(
            "Срок подтверждения переноса истёк. Открой /training_plan и попробуй ещё раз.",
            reply_markup=None,
        )
        await state.clear()
        return
    new_time = datetime.fromisoformat(new_time_raw)
    try:
        async with session_factory() as session:
            workout, reminder, changed, warnings = (
                await training_service.reschedule_and_rebalance(
                    session,
                    callback.from_user.id,
                    workout_id,
                    new_time,
                    time_service.now(),
                )
            )
            plan, sessions, workout = await training_service.get_workout_context(
                session,
                callback.from_user.id,
                workout.id,
            )
            await session.commit()
    except ValueError as exc:
        await callback.message.edit_text(str(exc), reply_markup=None)
        await state.clear()
        return
    await state.clear()
    for changed_reminder in [reminder, *(item[1] for item in changed)]:
        if changed_reminder:
            reminder_scheduler.cancel_reminder_job(changed_reminder.id)
            reminder_scheduler.schedule_reminder(changed_reminder)
    notice = f"Неделя пересобрана. Дополнительно перенесено тренировок: {len(changed)}."
    if warnings:
        notice += "\n\nНе удалось убрать все конфликты:\n" + "\n".join(
            f"• {escape(item)}" for item in warnings
        )
    await callback.message.edit_text(
        notice + "\n\n" + _format_training_plan(plan, sessions, workout),
        reply_markup=_training_week_markup(training_service, sessions, workout),
    )


@router.callback_query(F.data.startswith("workout_skip_menu:"))
async def workout_skip_menu(callback: CallbackQuery):
    await callback.answer()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    await callback.message.edit_reply_markup(reply_markup=workout_skip_keyboard(workout_id))


@router.callback_query(F.data.startswith("workout_skip_confirm:"))
async def workout_skip_confirm(
    callback: CallbackQuery,
    session_factory,
    training_service,
    reminder_scheduler,
):
    await callback.answer()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    async with session_factory() as session:
        workout, reminder = await training_service.skip_workout(
            session,
            callback.from_user.id,
            workout_id,
            "Пользователь пропустил тренировку",
        )
        plan, sessions, workout = await training_service.get_workout_context(
            session,
            callback.from_user.id,
            workout.id,
        )
        await session.commit()
    if reminder:
        reminder_scheduler.cancel_reminder_job(reminder.id)
    await callback.message.edit_text(
        f"Тренировка «{escape(workout.title)}» пропущена. Напоминание отменено.\n\n"
        "Остальные тренировки недели остаются на месте.\n\n"
        + _format_training_plan(plan, sessions, workout),
        reply_markup=_training_week_markup(training_service, sessions, workout),
    )


@router.callback_query(F.data.startswith("workout_done_menu:"))
async def workout_done_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    workout_id = int(callback.data.rsplit(":", 1)[1])
    await callback.message.edit_reply_markup(reply_markup=workout_rpe_keyboard(workout_id))


@router.callback_query(F.data.startswith("workout_done_rpe:"))
async def workout_done_rpe(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory,
    training_service,
    time_service,
    reminder_scheduler,
):
    await callback.answer()
    await state.clear()
    _, workout_id_raw, rpe_raw = callback.data.split(":")
    workout_id = int(workout_id_raw)
    try:
        async with session_factory() as session:
            logs = await training_service.get_set_logs(
                session,
                callback.from_user.id,
                workout_id,
            )
            workout, reminder = await training_service.complete_workout(
                session,
                callback.from_user.id,
                workout_id,
                time_service.now(),
                int(rpe_raw),
            )
            plan, sessions, workout = await training_service.get_workout_context(
                session,
                callback.from_user.id,
                workout.id,
            )
            await session.commit()
    except ValueError as exc:
        await callback.message.edit_text(str(exc), reply_markup=None)
        return
    if reminder:
        reminder_scheduler.cancel_reminder_job(reminder.id)
    logged_exercises = len({item.exercise_index for item in logs})
    log_note = (
        f"\nЗаписано: {logged_exercises} упр., {len(logs)} подходов."
        if logs
        else ""
    )
    await callback.message.edit_text(
        f"Тренировка выполнена. Нагрузка: RPE {workout.rpe}/10.{log_note}\n\n"
        "Следующие совпадающие упражнения адаптированы по фактическому выполнению.\n\n"
        + _format_training_plan(plan, sessions, workout),
        reply_markup=_training_week_markup(training_service, sessions, workout),
    )


@router.message(
    F.text.regexp(
        r"(?i)^(?!.*напомин).*(нач(ать|инаю|нём)|приступ(ить|аю|им))"
        r".{0,25}трениров"
    )
)
async def workout_start_text(
    message: Message,
    state: FSMContext,
    session_factory,
    training_service,
    time_service,
    reminder_scheduler,
    screen_service,
    bot,
):
    try:
        async with session_factory() as session:
            workout = await training_service.nearest_planned(
                session,
                message.from_user.id,
                time_service.now(),
            )
            reminder = None
            if workout.status == "planned":
                workout, reminder = await training_service.start_workout(
                    session,
                    message.from_user.id,
                    workout.id,
                    time_service.now(),
                )
                await session.commit()
    except ValueError as exc:
        await _render_training_screen(
            message, session_factory, screen_service, bot, str(exc)
        )
        return
    await state.clear()
    if reminder:
        reminder_scheduler.cancel_reminder_job(reminder.id)
    if training_service.current_exercise(workout) is None:
        text = (
            f"Тренировка «{escape(workout.title)}» идёт.\n\n"
            "Когда закончишь, оцени общую нагрузку."
        )
    else:
        text = _format_current_exercise(training_service, workout)
    await _render_training_screen(
        message,
        session_factory,
        screen_service,
        bot,
        text,
        reply_markup=_workout_action_keyboard(training_service, workout),
    )


@router.message(F.text.regexp(r"(?i)^(?!.*напомин).*(перенес\w*.{0,30}трениров|трениров\w*.{0,30}перенес)"))
async def workout_move_text(
    message,
    state,
    session_factory,
    training_service,
    time_service,
    screen_service,
    bot,
):
    try:
        async with session_factory() as session:
            workout = await training_service.nearest_planned(
                session, message.from_user.id, time_service.now()
            )
    except ValueError as exc:
        await _render_training_screen(
            message, session_factory, screen_service, bot, str(exc)
        )
        return
    await state.update_data(workout_id=workout.id)
    new_time = _workout_target_from_text(
        message.text or "", workout.scheduled_for, time_service
    )
    if new_time is None:
        await _training_prompt(
            message,
            state,
            TrainingRescheduleState.waiting_time,
            session_factory,
            screen_service,
            bot,
            "Когда перенести ближайшую тренировку?\n\nНапример: «завтра в 19:00».",
        )
        return
    async with session_factory() as session:
        conflicts = await training_service.analyze_reschedule(
            session,
            message.from_user.id,
            workout.id,
            new_time,
            time_service.now(),
        )
    await state.update_data(new_time=new_time.isoformat())
    await state.set_state(TrainingRescheduleState.confirming)
    await _render_training_screen(
        message,
        session_factory,
        screen_service,
        bot,
        _format_reschedule_preview(workout, new_time, conflicts),
        reply_markup=workout_move_confirm_keyboard(
            workout.id,
            has_conflicts=bool(conflicts),
        ),
    )


@router.message(F.text.regexp(r"(?i)^(?!.*напомин).*((пропус\w*|скип\w*).{0,30}трениров|трениров\w*.{0,30}(пропус\w*|скип\w*))"))
async def workout_skip_text(
    message,
    session_factory,
    training_service,
    time_service,
    screen_service,
    bot,
):
    try:
        async with session_factory() as session:
            workout = await training_service.nearest_planned(
                session, message.from_user.id, time_service.now()
            )
    except ValueError as exc:
        await _render_training_screen(
            message, session_factory, screen_service, bot, str(exc)
        )
        return
    await _render_training_screen(
        message,
        session_factory,
        screen_service,
        bot,
        f"Пропустить тренировку?\n\n"
        f"{workout.scheduled_for.strftime('%d.%m в %H:%M')} — {escape(workout.title)}\n\n"
        "Напоминание будет отменено, остальные занятия не сдвинутся.",
        reply_markup=workout_skip_keyboard(workout.id),
    )


def _workout_target_from_text(raw: str, current: datetime, time_service) -> datetime | None:
    destination_marker = raw.lower().rfind(" на ")
    destination = raw[destination_marker + 4:] if destination_marker >= 0 else raw
    explicit_date = time_service.extract_date_from_text(destination)
    explicit_time = time_service.extract_time_from_text(destination)
    current = current if current.tzinfo else current.replace(tzinfo=time_service.tz)
    if explicit_date or explicit_time:
        target_date = explicit_date or time_service.today()
        target_time = explicit_time or current.timetz().replace(tzinfo=None)
        target = datetime.combine(target_date, target_time, tzinfo=time_service.tz)
        if explicit_date is None and target <= time_service.now():
            target += timedelta(days=1)
        return target
    return time_service.parse_snooze_text(raw)


def _format_reschedule_preview(workout, new_time: datetime, conflicts: list[str]) -> str:
    lines = [
        "Перенести тренировку?",
        "",
        f"Было: {workout.scheduled_for.strftime('%d.%m в %H:%M')}",
        f"Станет: {new_time.strftime('%d.%m в %H:%M')}",
    ]
    if workout.is_fixed:
        lines += ["", "Это фиксированное занятие — проверь, что его время действительно изменилось."]
    if conflicts:
        lines += ["", "Конфликт нагрузки:"]
        lines.extend(f"• {escape(item)}" for item in conflicts)
        lines += [
            "",
            "Можно перенести только это занятие или пересобрать остальные гибкие тренировки.",
        ]
    else:
        lines += [
            "",
            "Конфликтов не найдено. Остальную неделю можно оставить или пересобрать.",
        ]
    return "\n".join(lines)
