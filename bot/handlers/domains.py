from __future__ import annotations

import json
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.database.queries import (
    create_exam_date,
    create_reminder,
    create_schedule_override,
    create_study_schedule_item,
    create_task,
    find_task_by_title,
    get_active_tasks,
    get_active_exam_dates,
    get_active_study_schedule,
    get_pending_preview,
    get_or_create_runtime_state,
    get_reminder_by_id,
    get_problem_block_by_id,
    upsert_exam_date_smart,
    archive_exam_date,
    archive_study_schedule_item,
)
from bot.services.subject_normalizer import normalize_subject, find_best_exam_match
from bot.keyboards.inline import overload_keyboard, problem_block_keyboard, problem_snooze_keyboard, reminder_snooze_keyboard

router = Router()


# ── Action Preview ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm_preview:"))
async def confirm_preview(callback: CallbackQuery, session_factory, reminder_scheduler, time_service, problem_block_service, problem_resources_service):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if not preview or preview.status != "pending":
            await callback.message.edit_text("Preview не найден или уже обработан.", reply_markup=None)
            return

        payload = json.loads(preview.preview_json)
        created_tasks_by_title: dict = {}
        created_reminders = []
        created_problem_blocks = []
        study_created_exams: int = 0
        study_updated_exams: list = []
        study_deleted_exams: list = []
        study_deleted_schedule: list = []

        for intent in payload.get("intents", []):
            t = intent.get("type")
            if t == "create_task":
                task = await create_task(
                    session, callback.from_user.id,
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
                    session, callback.from_user.id,
                    intent.get("text") or "Напоминание",
                    remind_at=remind_at,
                    priority=intent.get("priority") or "medium",
                    related_entity_type=related_entity_type,
                    related_entity_id=related_entity_id,
                )
                created_reminders.append(reminder)

            elif t in {"schedule_override", "rest_day"}:
                await create_schedule_override(
                    session, callback.from_user.id,
                    datetime.fromisoformat(intent["override_date"]).date(),
                    mode=intent.get("mode") or "rest_day",
                    create_tasks=bool(intent.get("create_tasks", False)),
                    write_to_miro=bool(intent.get("write_to_miro", False)),
                )

            elif t == "create_problem_block":
                resources = problem_resources_service.get_resources(
                    intent.get("category") or "other",
                    intent.get("title") or "",
                    intent.get("problem_text") or "",
                )
                block = await problem_block_service.create_problem_block(
                    callback.from_user.id, intent, session, time_service.now(),
                    source_type=preview.source_type, resources=resources,
                )
                created_problem_blocks.append(block)

            elif t == "set_exam_date":
                subject = normalize_subject(intent.get("subject") or "Предмет")
                exam_date_str = intent.get("exam_date") or ""
                if exam_date_str:
                    from datetime import date as _date
                    try:
                        exam_date = _date.fromisoformat(exam_date_str)
                        _, op = await upsert_exam_date_smart(session, callback.from_user.id, subject, exam_date)
                        if op == "created":
                            study_created_exams += 1
                        else:
                            study_updated_exams.append(subject)
                    except ValueError:
                        import logging
                        logging.getLogger(__name__).warning("Invalid exam_date: %s", exam_date_str)

            elif t == "update_exam_date":
                target_subject = normalize_subject(intent.get("target_subject") or intent.get("subject") or "")
                new_date_str = intent.get("new_date") or intent.get("exam_date") or ""
                if target_subject and new_date_str:
                    from datetime import date as _date
                    try:
                        new_date = _date.fromisoformat(new_date_str)
                        # Find existing exam by canonical subject
                        res_exams = await get_active_exam_dates(session, callback.from_user.id)
                        all_exams = await session.execute(__import__('sqlalchemy').select(__import__('bot.database.models', fromlist=['ExamDate']).ExamDate).where(__import__('bot.database.models', fromlist=['ExamDate']).ExamDate.user_id == callback.from_user.id))
                        target_exam = find_best_exam_match(res_exams, target_subject)
                        if target_exam:
                            target_exam.exam_date = new_date
                            target_exam.status = "active"
                            await session.flush()
                            study_updated_exams.append(target_subject)
                        else:
                            # Fallback: create as new
                            await upsert_exam_date_smart(session, callback.from_user.id, target_subject, new_date)
                            study_created_exams += 1
                    except Exception as exc:
                        import logging
                        logging.getLogger(__name__).warning("update_exam_date failed: %s", exc)

            elif t == "delete_exam_date":
                target_subject = normalize_subject(intent.get("target_subject") or intent.get("subject") or "")
                if target_subject:
                    from sqlalchemy import select as _sel
                    from bot.database.models import ExamDate as _ExamDate
                    res_all = await session.execute(
                        _sel(_ExamDate).where(_ExamDate.user_id == callback.from_user.id, _ExamDate.status == "active")
                    )
                    all_active = list(res_all.scalars().all())
                    target_exam = find_best_exam_match(all_active, target_subject)
                    if target_exam:
                        await archive_exam_date(session, callback.from_user.id, target_exam.id, "deleted by user")
                        study_deleted_exams.append(target_subject)

            elif t == "delete_study_schedule_item":
                target_subject = normalize_subject(intent.get("target_subject") or intent.get("subject") or "")
                target_weekday = intent.get("weekday") or ""
                if target_subject:
                    from sqlalchemy import select as _sel, and_ as _and
                    from bot.database.models import StudyScheduleItem as _SSI
                    q = _sel(_SSI).where(_and(_SSI.user_id == callback.from_user.id, _SSI.status == "active"))
                    res_sched = await session.execute(q)
                    items = list(res_sched.scalars().all())
                    norm_target = normalize_subject(target_subject)
                    for item in items:
                        if normalize_subject(item.subject) == norm_target:
                            if not target_weekday or item.weekday == target_weekday:
                                await archive_study_schedule_item(session, callback.from_user.id, item.id, "deleted by user")
                                study_deleted_schedule.append(f"{item.subject} {item.weekday}")
                                break

            elif t == "create_study_schedule_item":
                subject = intent.get("subject") or "Предмет"
                await create_study_schedule_item(
                    session, callback.from_user.id, subject,
                    title=intent.get("title") or f"{subject} — занятие",
                    weekday=intent.get("weekday") or "mon",
                    time_str=intent.get("time_str") or "",
                    recurrence=intent.get("recurrence") or "weekly",
                    tutor_name=intent.get("tutor_name") or "",
                    location_or_link=intent.get("location_or_link") or "",
                )

        preview.status = "confirmed"
        await session.commit()

    for reminder in created_reminders:
        if reminder.remind_at > time_service.now():
            reminder_scheduler.schedule_reminder(reminder)

    # ── Determine result text ─────────────────────────────────────────────────
    intents_types = {i.get("type") for i in payload.get("intents", [])}
    has_study = bool(intents_types & {"set_exam_date", "create_study_schedule_item",
                                      "update_exam_date", "delete_exam_date", "delete_study_schedule_item"})
    if has_study or created_problem_blocks:
        # Build detailed result with created/updated/total counts
        result_lines = ["Готово.\n"]

        # Exam changes
        if study_created_exams > 0:
            result_lines.append(f"Добавлено экзаменов: {study_created_exams}")
        if study_updated_exams:
            result_lines.append("Обновлено:")
            for subj in study_updated_exams:
                result_lines.append(f"  • {subj}")
        if study_deleted_exams:
            result_lines.append("Убрано:")
            for subj in study_deleted_exams:
                result_lines.append(f"  • {subj}")
        if study_deleted_schedule:
            result_lines.append("Занятий убрано:")
            for s in study_deleted_schedule:
                result_lines.append(f"  • {s}")

        # Schedule creates
        n_sched = sum(1 for i in payload.get("intents", []) if i.get("type") == "create_study_schedule_item")
        if n_sched:
            result_lines.append(f"Занятий добавлено: {n_sched}")

        # Problem blocks
        if created_problem_blocks:
            result_lines.append(f"Блоков: {len(created_problem_blocks)}")
            for b in created_problem_blocks[:2]:
                result_lines.append(f"  • {b.title}")
                result_lines.append(f"    Шаг: {b.next_action[:60]}")
                result_lines.append("    Подробный план — в Miro после /sync_miro")

        # Totals (fetched fresh from DB)
        try:
            async with session_factory() as _s:
                _active_exams = await get_active_exam_dates(_s, callback.from_user.id)
                _active_sched = await get_active_study_schedule(_s, callback.from_user.id)
            result_lines.append("")
            result_lines.append(f"Активных экзаменов: {len(_active_exams)}")
            result_lines.append(f"Занятий: {len(_active_sched)}")
        except Exception:
            pass

        result_lines.append("\nСмотри: /study")
        result_text = "\n".join(result_lines)
    elif created_reminders:
        if any(r.remind_at <= time_service.now() for r in created_reminders):
            result_text = "Напоминание создано, но время уже прошло. Проверь дату/время."
        else:
            result_text = "Готово. Напоминание создано."
    elif any(i.get("type") in {"schedule_override", "rest_day"} for i in payload.get("intents", [])):
        result_text = "Готово, день отдыха сохранён."
    else:
        result_text = "Готово. Задача создана."

    # Edit the preview message — remove keyboard, show result
    await callback.message.edit_text(result_text, reply_markup=None)

    # For problem blocks — send a separate action message (not a screen)
    for block in created_problem_blocks[:1]:
        await callback.message.answer(
            "Фиксирую проблему.\nЭто активный блок.\n\nРешение коротко:\n"
            + problem_block_service.build_problem_solution_summary(block),
            reply_markup=problem_block_keyboard(block.id),
        )


@router.callback_query(F.data.startswith("cancel_preview:"))
async def cancel_preview(callback: CallbackQuery, session_factory):
    await callback.answer()
    preview_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        preview = await get_pending_preview(session, preview_id, callback.from_user.id)
        if preview:
            preview.status = "cancelled"
            await session.commit()
    # Edit preview message — remove keyboard, show cancellation
    await callback.message.edit_text("Отменено.", reply_markup=None)


# ── Problem block callbacks ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("problem_done:"))
async def problem_done(callback: CallbackQuery, session_factory, problem_block_service, time_service):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        block = await problem_block_service.complete_problem_block(callback.from_user.id, block_id, session, time_service.now())
        await session.commit()
    await callback.message.edit_text("Блок закрыт." if block else "Блок не найден.", reply_markup=None)


@router.callback_query(F.data.startswith("problem_archive:"))
async def problem_archive(callback: CallbackQuery, session_factory, problem_block_service, time_service):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        block = await problem_block_service.archive_problem_block(callback.from_user.id, block_id, "manual archive", session, time_service.now())
        await session.commit()
    await callback.message.edit_text("Блок в архиве." if block else "Блок не найден.", reply_markup=None)


@router.callback_query(F.data.startswith("problem_resources:"))
async def problem_resources(callback: CallbackQuery, session_factory):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        block = await get_problem_block_by_id(session, callback.from_user.id, block_id)
    if not block:
        await callback.message.answer("Блок не найден.")
        return
    data = json.loads(block.resources_json or "[]")
    if not data:
        await callback.message.answer("Готовых ресурсов нет.")
        return
    await callback.message.answer("Ресурсы:\n" + "\n".join(f"- {r.get('title')}: {r.get('note')}" for r in data[:4]))


@router.callback_query(F.data.startswith("problem_expand:"))
async def problem_expand(callback: CallbackQuery, session_factory, problem_block_service):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        block = await get_problem_block_by_id(session, callback.from_user.id, block_id)
    if not block or block.status != "active":
        await callback.message.answer("Блок не найден или уже закрыт.")
        return
    resources = json.loads(block.resources_json or "[]")
    text = (
        f"ПРОБЛЕМА\n{block.title}\n\n"
        "ПОЧЕМУ МЕШАЕТ\nСоздаёт повторяемый сбой и съедает ресурс.\n\n"
        + problem_block_service.build_problem_solution_summary(block)
    )
    if resources:
        text += "\n\nРЕСУРСЫ\n" + "\n".join(f"- {r.get('title')}" for r in resources[:3])
    await callback.message.edit_text(text, reply_markup=problem_block_keyboard(block.id))


@router.callback_query(F.data.startswith("problem_snooze:"))
async def problem_snooze(callback: CallbackQuery):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    await callback.message.edit_reply_markup(reply_markup=problem_snooze_keyboard(block_id))


@router.callback_query(F.data.startswith("problem_snooze_tomorrow:"))
async def problem_snooze_tomorrow(callback: CallbackQuery, session_factory, problem_block_service, time_service):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    until = time_service.build_datetime("tomorrow", "morning").replace(hour=9, minute=0, second=0, microsecond=0)
    async with session_factory() as session:
        item = await problem_block_service.snooze_problem_block(callback.from_user.id, block_id, until, session, "Snoozed until tomorrow")
        await session.commit()
    await callback.message.edit_text("Отложил до завтра, 09:00." if item else "Блок не найден.", reply_markup=None)


@router.callback_query(F.data.startswith("problem_snooze_3d:"))
async def problem_snooze_3d(callback: CallbackQuery, session_factory, problem_block_service, time_service):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        item = await problem_block_service.snooze_problem_block(callback.from_user.id, block_id, time_service.now() + timedelta(days=3), session, "Snoozed for 3 days")
        await session.commit()
    await callback.message.edit_text("Отложил на 3 дня." if item else "Блок не найден.", reply_markup=None)


@router.callback_query(F.data.startswith("problem_snooze_week:"))
async def problem_snooze_week(callback: CallbackQuery, session_factory, problem_block_service, time_service):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        item = await problem_block_service.snooze_problem_block(callback.from_user.id, block_id, time_service.now() + timedelta(days=7), session, "Snoozed for 7 days")
        await session.commit()
    await callback.message.edit_text("Отложил на неделю." if item else "Блок не найден.", reply_markup=None)


@router.callback_query(F.data.startswith("problem_snooze_cancel:"))
async def problem_snooze_cancel(callback: CallbackQuery, session_factory):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        block = await get_problem_block_by_id(session, callback.from_user.id, block_id)
    await callback.message.edit_reply_markup(reply_markup=problem_block_keyboard(block_id) if block else None)


@router.callback_query(F.data.startswith("problem_keep_active:"))
async def problem_keep_active(callback: CallbackQuery, session_factory):
    await callback.answer()
    block_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        block = await get_problem_block_by_id(session, callback.from_user.id, block_id)
        if block:
            block.status = "active"
        await session.commit()
    await callback.message.edit_text("Оставил активным." if block else "Блок не найден.", reply_markup=None)


# ── Quiet mode ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "quiet_mode")
async def quiet_mode(callback: CallbackQuery):
    await callback.answer()
    from bot.keyboards.inline import quiet_keyboard
    await callback.message.answer("Тихий режим:", reply_markup=quiet_keyboard())


@router.callback_query(F.data == "quiet_2h")
async def quiet_2h(callback: CallbackQuery, session_factory, checkin_service):
    await callback.answer()
    async with session_factory() as session:
        await checkin_service.set_quiet_2h(callback.from_user.id, session)
        await session.commit()
    await callback.message.edit_text("Тихий режим на 2 часа.", reply_markup=None)


@router.callback_query(F.data == "quiet_until_tomorrow")
async def quiet_until_tomorrow(callback: CallbackQuery, session_factory, checkin_service):
    await callback.answer()
    async with session_factory() as session:
        await checkin_service.set_quiet_until_tomorrow(callback.from_user.id, session)
        await session.commit()
    await callback.message.edit_text("Тихий режим до завтра.", reply_markup=None)


@router.callback_query(F.data == "checkins_disable")
async def checkins_disable(callback: CallbackQuery, session_factory, checkin_service):
    await callback.answer()
    async with session_factory() as session:
        await checkin_service.disable_checkins(callback.from_user.id, session)
        await session.commit()
    await callback.message.edit_text("Плановые проверки выключены.", reply_markup=None)


@router.callback_query(F.data == "quiet_cancel")
async def quiet_cancel(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("Отменено.", reply_markup=None)


# ── Check-in callbacks ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "checkin_all_ok")
async def checkin_all_ok(callback: CallbackQuery, session_factory, time_service):
    await callback.answer()
    async with session_factory() as session:
        state = await get_or_create_runtime_state(session, callback.from_user.id)
        state.last_checkin_at = time_service.now()
        await session.commit()
    await callback.message.edit_text("Принял. День идёт планово.", reply_markup=None)


@router.callback_query(F.data.in_({"checkin_build_day", "checkin_next_step"}))
async def checkin_next_step(callback: CallbackQuery, session_factory, time_service, next_step_service):
    await callback.answer()
    async with session_factory() as session:
        payload = await next_step_service.build_next_step(callback.from_user.id, session, time_service.now())
    body = "\n".join(f"{i+1}. {a}" for i, a in enumerate(payload.get("actions", [])[:3])) or "1. Один короткий шаг."
    await callback.message.edit_text(f"Следующий шаг:\n\n{body}", reply_markup=None)


@router.callback_query(F.data == "checkin_overload")
async def checkin_overload(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "ПЕРЕГРУЗ\n\nРесурс просел.\n1. Вода.\n2. Еда.\n3. 20–40 минут отдыха без телефона.",
        reply_markup=overload_keyboard(),
    )


@router.callback_query(F.data == "checkin_sleep_bad")
async def checkin_sleep_bad(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "Сон просел.\n\nСегодня без добивания.\n1. Вода.\n2. Еда.\n3. Одна главная задача.",
        reply_markup=None,
    )


@router.callback_query(F.data == "checkin_close_day")
async def checkin_close_day(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "Закрываем день.\n\nЧто фиксируем?\n1. Сделано\n2. Перенести\n3. Сон/тело\n4. Завтрашний фокус",
        reply_markup=None,
    )


@router.callback_query(F.data == "checkin_move_tasks")
async def checkin_move_tasks(callback: CallbackQuery, session_factory):
    await callback.answer()
    async with session_factory() as session:
        tasks = await get_active_tasks(session, callback.from_user.id)
    lines = [f"- {t.title}" for t in tasks[:10]] or ["- нет активных задач"]
    await callback.message.edit_text(
        "Активные задачи:\n" + "\n".join(lines) + "\n\nПодтверди перенос отдельным сообщением.",
        reply_markup=None,
    )


# ── Reminder callbacks ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("reminder_done:"))
async def reminder_done(callback: CallbackQuery, session_factory, reminder_scheduler):
    await callback.answer()
    reminder_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        reminder = await get_reminder_by_id(session, callback.from_user.id, reminder_id)
        if not reminder:
            await callback.message.edit_text("Напоминание не найдено.", reply_markup=None)
            return
        reminder.status = "done"
        await session.commit()
    reminder_scheduler.cancel_reminder_job(reminder_id)
    await callback.message.edit_text("Готово. Напоминание закрыто.", reply_markup=None)


@router.callback_query(F.data.startswith("reminder_cancel:"))
async def reminder_cancel(callback: CallbackQuery, session_factory, reminder_scheduler):
    await callback.answer()
    reminder_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        reminder = await get_reminder_by_id(session, callback.from_user.id, reminder_id)
        if not reminder:
            await callback.message.edit_text("Напоминание не найдено.", reply_markup=None)
            return
        reminder.status = "cancelled"
        await session.commit()
    reminder_scheduler.cancel_reminder_job(reminder_id)
    await callback.message.edit_text("Напоминание отменено.", reply_markup=None)


@router.callback_query(F.data.startswith("reminder_snooze_menu:"))
async def reminder_snooze_menu(callback: CallbackQuery):
    await callback.answer()
    reminder_id = int(callback.data.split(":", 1)[1])
    await callback.message.edit_reply_markup(reply_markup=reminder_snooze_keyboard(reminder_id))


async def _apply_snooze(callback: CallbackQuery, session_factory, reminder_scheduler, time_service, reminder_id: int, mode: str):
    async with session_factory() as session:
        reminder = await get_reminder_by_id(session, callback.from_user.id, reminder_id)
        if not reminder:
            await callback.message.edit_text("Напоминание не найдено.", reply_markup=None)
            return
    now = time_service.now()
    if mode == "1h":
        new_time = now + timedelta(hours=1)
    elif mode == "evening":
        candidate = time_service.build_datetime("today", "evening")
        new_time = candidate if candidate > now else time_service.build_datetime("tomorrow", "evening")
    else:
        new_time = time_service.build_datetime("tomorrow", "morning")
    updated = await reminder_scheduler.reschedule_reminder(reminder_id, new_time)
    if not updated:
        await callback.message.edit_text("Напоминание не найдено.", reply_markup=None)
        return
    await callback.message.edit_text(f"Перенёс на {updated.remind_at.strftime('%H:%M')}.", reply_markup=None)


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
    reminder_id = int(callback.data.split(":", 1)[1])
    from bot.keyboards.inline import reminder_keyboard
    await callback.message.edit_reply_markup(reply_markup=reminder_keyboard(reminder_id))


# ── Tasks ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("task_done:"))
async def task_done_callback(callback: CallbackQuery, session_factory, time_service):
    await callback.answer()
    task_id = int(callback.data.split(":", 1)[1])
    async with session_factory() as session:
        from sqlalchemy import select
        from bot.database.models import Task
        res = await session.execute(
            select(Task).where(Task.id == task_id, Task.user_id == callback.from_user.id)
        )
        task = res.scalar_one_or_none()
        if not task:
            await callback.message.edit_text("Задача не найдена.", reply_markup=None)
            return
        task.status = "done"
        task.completed_at = time_service.now()
        await session.commit()
    await callback.message.edit_text("Принял. Задача закрыта.", reply_markup=None)


# ── Utility commands ───────────────────────────────────────────────────────────

@router.message(Command("cleanup"))
async def cleanup(message: Message, session_factory, cleanup_service, time_service, screen_service):
    async with session_factory() as session:
        archived = await cleanup_service.run(session, message.from_user.id, time_service.now())
        await session.commit()
    if not archived:
        await message.answer("Убрано в архив: 0 задач")
    else:
        await message.answer("Убрано в архив: {} задач\n{}".format(
            len(archived), "\n".join(f"- {t.title}" for t in archived)
        ))
    await screen_service.delete_user_input(message)


@router.message(Command("sync_miro"))
async def sync_miro(message: Message, session_factory, miro_service, time_service, config, next_step_service, screen_service):
    if not miro_service.is_configured():
        await message.answer("Miro не настроен.\n\nНужно заполнить:\nMIRO_ACCESS_TOKEN\nMIRO_BOARD_ID")
        await screen_service.delete_user_input(message)
        return
    try:
        async with session_factory() as session:
            stats = await miro_service.sync_all(
                message.from_user.id, session, time_service, config,
                next_step_service=next_step_service,
            )
            await session.commit()
        errors = stats.get("errors", 0)
        total_items = stats.get("created", 0) + stats.get("updated", 0)
        failed_sections = stats.get("failed_sections", [])

        if total_items == 0 and errors > 0:
            result_header = "Miro не обновлён."
        elif errors > 0:
            result_header = "Miro обновлён частично."
        else:
            result_header = "Miro обновлён."

        lines = [
            result_header, "",
            f"Секции: {stats.get('sections', 0)}",
            f"Карточки: {stats.get('cards', 0)}",
            f"Создано: {stats.get('created', 0)}",
            f"Обновлено: {stats.get('updated', 0)}",
            f"Ошибки: {errors}",
        ]

        if stats.get("exams", 0) or stats.get("schedule_items", 0) or stats.get("study_blocks", 0):
            lines += [
                "",
                "Учёба:",
                f"Экзаменов: {stats.get('exams', 0)}",
                f"Занятий: {stats.get('schedule_items', 0)}",
                f"Блоков: {stats.get('study_blocks', 0)}",
            ]

        if failed_sections:
            lines += ["", "Проблемные секции:"]
            for s in failed_sections:
                lines.append(f"  — {s}")
            lines.append("\nСмотри логи:\njournalctl -u 813assistant -n 120 --no-pager")
        elif errors > 0:
            lines.append("\nСмотри логи:\njournalctl -u 813assistant -n 120 --no-pager")
        else:
            lines.append("\nПроверь доску в Miro.")

        await message.answer("\n".join(lines))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("sync_miro unexpected error")
        await message.answer("Miro: критическая ошибка синхронизации. Проверь логи.\n/miro_debug — диагностика.")
    await screen_service.delete_user_input(message)



# ── Overload callbacks ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "overload_rest")
async def overload_rest(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "Принял. Экстренный отдых: 20–40 минут без телефона. Потом вернёмся к плану.",
        reply_markup=None,
    )


@router.callback_query(F.data == "overload_light_plan")
async def overload_light_plan(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "СИТУАЦИЯ\nРесурс просел.\n\nВЫВОД\nРаботаем в лёгком режиме.\n\n"
        "ДЕЙСТВИЕ\n1. Закрыть один обязательный пункт.\n2. Пауза 20 минут.\n3. Вернуться к следующему шагу.",
        reply_markup=None,
    )


@router.callback_query(F.data == "overload_skip")
async def overload_skip(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "Принял. Риск перегруза сохраняется.\n\n"
        "1. Закрыть критичный хвост.\n2. Один учебный блок.\n3. Перепроверить ресурс через 2 часа.\n\n"
        "Ограничение: без лишних задач.",
        reply_markup=None,
    )


# ── Menu navigation callbacks (kept for backward compat from old inline buttons) ──

@router.callback_query(F.data == "go_hq")
async def go_hq_callback(callback: CallbackQuery, session_factory, time_service, navigation_service, screen_service, bot):
    await callback.answer()
    navigation_service.push(callback.from_user.id, "hq")
    from bot.handlers.menu import render_hq
    # Use callback.message as message proxy for render_hq
    await render_hq(callback.message, session_factory, time_service, screen_service, bot)


@router.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, navigation_service):
    await callback.answer()
    navigation_service.reset(callback.from_user.id)
    # Just dismiss the inline keyboard, main menu is always in bottom ReplyKeyboard
    await callback.message.edit_reply_markup(reply_markup=None)


# ── Dedupe study callback ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dedupe_archive:"))
async def dedupe_archive_callback(callback: CallbackQuery, session_factory, config):
    """Handle dedupe confirmation: archive the duplicate exam_date."""
    await callback.answer()
    if callback.from_user.id != config.allowed_user_id:
        return
    parts = callback.data.split(":")
    # dedupe_archive:{duplicate_id}:{keep_id}
    if len(parts) < 3:
        await callback.message.edit_text("Ошибка: неверный формат.", reply_markup=None)
        return
    dup_id = int(parts[1])
    keep_id = int(parts[2])
    async with session_factory() as session:
        dup = await archive_exam_date(session, callback.from_user.id, dup_id, "deduplicated")
        from bot.database.queries import get_exam_date_by_id
        kept = await get_exam_date_by_id(session, callback.from_user.id, keep_id)
        await session.commit()
    if dup:
        kept_subj = kept.subject if kept else "?"
        await callback.message.edit_text(
            f"Дубль убран: {dup.subject}\nОстаётся: {kept_subj}\n\nПроверь: /study",
            reply_markup=None,
        )
    else:
        await callback.message.edit_text("Экзамен не найден.", reply_markup=None)


@router.callback_query(F.data.startswith("dedupe_keep:"))
async def dedupe_keep_callback(callback: CallbackQuery):
    """Handle dedupe skip: keep both exams."""
    await callback.answer()
    await callback.message.edit_text("Оставляю оба. Если нужно — удали вручную через /dedupe_study.", reply_markup=None)
