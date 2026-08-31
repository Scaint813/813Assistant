from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from bot.database.models import (
    HealthNudge,
    ProblemBlock,
    Reminder,
    Task,
    WorkoutSession,
)
from bot.database.queries import get_or_create_runtime_state
from bot.services.datetime_utils import ensure_aware

router = Router()


def _inbox_keyboard(task: Task) -> InlineKeyboardMarkup:
    rows = []
    if not task.duration_confirmed:
        rows += [[
            InlineKeyboardButton(text="15 мин", callback_data=f"inbox_duration:{task.id}:15"),
            InlineKeyboardButton(text="30 мин", callback_data=f"inbox_duration:{task.id}:30"),
            InlineKeyboardButton(text="60 мин", callback_data=f"inbox_duration:{task.id}:60"),
        ]]
    if not task.deadline_confirmed:
        rows += [[
            InlineKeyboardButton(text="Сегодня", callback_data=f"inbox_deadline:{task.id}:today"),
            InlineKeyboardButton(text="Завтра", callback_data=f"inbox_deadline:{task.id}:tomorrow"),
            InlineKeyboardButton(text="Без срока", callback_data=f"inbox_deadline:{task.id}:none"),
        ]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _first_inbox(session, user_id: int) -> Task | None:
    result = await session.execute(
        select(Task).where(
            Task.user_id == user_id,
            Task.status == "active",
            Task.planning_state == "inbox",
        ).order_by(Task.created_at.asc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _inbox_text(session, user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    count_result = await session.execute(
        select(func.count(Task.id)).where(
            Task.user_id == user_id, Task.status == "active", Task.planning_state == "inbox"
        )
    )
    count = int(count_result.scalar_one() or 0)
    task = await _first_inbox(session, user_id)
    if not task:
        return "Входящие разобраны. Все задачи либо запланированы, либо закрыты.", None
    known = []
    if task.duration_confirmed:
        known.append(f"длительность {task.estimated_minutes} мин")
    if task.deadline_confirmed:
        known.append("срок указан" if task.deadline else "без срока")
    lines = [
        f"Входящие · осталось {count}", "", escape(task.title), "",
        "Чтобы задача попала в реалистичный план, нужны длительность и срок.",
    ]
    if known:
        lines += ["Уже известно: " + ", ".join(known) + "."]
    return "\n".join(lines), _inbox_keyboard(task)


@router.message(Command("inbox"))
async def inbox(message: Message, session_factory, screen_service):
    async with session_factory() as session:
        text, keyboard = await _inbox_text(session, message.from_user.id)
    await message.answer(text, reply_markup=keyboard)
    await screen_service.delete_user_input(message)


@router.callback_query(F.data == "nav_inbox")
async def nav_inbox(callback: CallbackQuery, session_factory):
    async with session_factory() as session:
        text, keyboard = await _inbox_text(session, callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("inbox_duration:"))
async def inbox_duration(callback: CallbackQuery, session_factory, metric_service):
    _, task_id, minutes = callback.data.split(":")
    async with session_factory() as session:
        task = await session.get(Task, int(task_id))
        if not task or task.user_id != callback.from_user.id or task.status != "active":
            await callback.answer("Задача не найдена", show_alert=True)
            return
        task.estimated_minutes = int(minutes)
        task.duration_confirmed = True
        if task.deadline_confirmed:
            task.planning_state = "ready"
        await metric_service.record(session, callback.from_user.id, "inbox_duration_set", entity_type="task", entity_id=task.id)
        await session.commit()
        text, keyboard = await _inbox_text(session, callback.from_user.id)
    await callback.answer("Длительность сохранена")
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("inbox_deadline:"))
async def inbox_deadline(callback: CallbackQuery, session_factory, time_service, metric_service):
    _, task_id, choice = callback.data.split(":")
    now = time_service.now()
    async with session_factory() as session:
        task = await session.get(Task, int(task_id))
        if not task or task.user_id != callback.from_user.id or task.status != "active":
            await callback.answer("Задача не найдена", show_alert=True)
            return
        if choice == "today":
            task.deadline = datetime.combine(now.date(), time(20), tzinfo=now.tzinfo)
        elif choice == "tomorrow":
            task.deadline = datetime.combine(now.date() + timedelta(days=1), time(20), tzinfo=now.tzinfo)
        else:
            task.deadline = None
        task.deadline_confirmed = True
        if task.duration_confirmed:
            task.planning_state = "ready"
        await metric_service.record(session, callback.from_user.id, "inbox_deadline_set", entity_type="task", entity_id=task.id)
        await session.commit()
        text, keyboard = await _inbox_text(session, callback.from_user.id)
    await callback.answer("Срок сохранён")
    await callback.message.edit_text(text, reply_markup=keyboard)


def _focus_start_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="25 минут", callback_data=f"focus_start:{task_id}:25"),
        InlineKeyboardButton(text="50 минут", callback_data=f"focus_start:{task_id}:50"),
    ]])


def _focus_active_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Завершить задачу", callback_data=f"focus_finish:{session_id}:completed")],
        [InlineKeyboardButton(text="Ещё 15 минут", callback_data=f"focus_finish:{session_id}:extend"),
         InlineKeyboardButton(text="Есть препятствие", callback_data=f"focus_finish:{session_id}:blocked")],
    ])


@router.message(Command("focus"))
async def focus(message: Message, session_factory, focus_service, time_service, screen_service):
    async with session_factory() as session:
        active = await focus_service.active(session, message.from_user.id)
        if active:
            task = await session.get(Task, active.task_id)
            text = (
                f"Фокус уже идёт: {escape(task.title) if task else 'задача'}\n"
                f"Начат в {ensure_aware(active.started_at, time_service.now().tzinfo).strftime('%H:%M')}."
            )
            keyboard = _focus_active_keyboard(active.id)
        else:
            task = await focus_service.pick_task(session, message.from_user.id, time_service.now())
            if task:
                text = f"Фокус\n\n{escape(task.title)}\n\nНа сколько времени отключаем остальные задачи?"
                keyboard = _focus_start_keyboard(task.id)
            else:
                text, keyboard = "Нет готовой задачи для фокуса. Сначала попроси: «разберём входящие».", None
    await message.answer(text, reply_markup=keyboard)
    await screen_service.delete_user_input(message)


@router.callback_query(F.data == "nav_focus")
async def nav_focus(callback: CallbackQuery, session_factory, focus_service, time_service):
    async with session_factory() as session:
        active = await focus_service.active(session, callback.from_user.id)
        if active:
            task = await session.get(Task, active.task_id)
            text = f"Фокус уже идёт: {escape(task.title) if task else 'задача'}."
            keyboard = _focus_active_keyboard(active.id)
        else:
            task = await focus_service.pick_task(session, callback.from_user.id, time_service.now())
            text = (
                f"Фокус\n\n{escape(task.title)}\n\nНа сколько времени?"
                if task else "Нет готовой задачи. Сначала разбери Входящие."
            )
            keyboard = _focus_start_keyboard(task.id) if task else None
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("focus_start:"))
async def focus_start(callback: CallbackQuery, session_factory, focus_service, time_service, metric_service):
    _, task_id, minutes = callback.data.split(":")
    async with session_factory() as session:
        focus = await focus_service.start(
            session, callback.from_user.id, int(task_id), time_service.now(), int(minutes)
        )
        if not focus:
            await callback.answer("Задача не найдена", show_alert=True)
            return
        task = await session.get(Task, focus.task_id)
        await metric_service.record(session, callback.from_user.id, "focus_started", entity_type="task", entity_id=focus.task_id)
        await session.commit()
    await callback.answer("Фокус начат")
    await callback.message.edit_text(
        f"Фокус на {focus.planned_minutes} минут\n\n{escape(task.title)}\n\n"
        "Сейчас важна только эта задача. Когда остановишься — выбери фактический результат.",
        reply_markup=_focus_active_keyboard(focus.id),
    )


@router.callback_query(F.data.startswith("focus_finish:"))
async def focus_finish(callback: CallbackQuery, session_factory, focus_service, time_service, metric_service, miro_sync_coordinator):
    _, _session_id, action = callback.data.split(":")
    async with session_factory() as session:
        focus, task = await focus_service.finish(session, callback.from_user.id, time_service.now(), action)
        if not focus:
            await callback.answer("Активная сессия не найдена", show_alert=True)
            return
        await metric_service.record(session, callback.from_user.id, f"focus_{action}", entity_type="task", entity_id=focus.task_id)
        await session.commit()
    await callback.answer()
    if action == "extend":
        await callback.message.edit_text(
            f"Продолжаем. Всего запланировано {focus.planned_minutes} минут.\n\n{escape(task.title)}",
            reply_markup=_focus_active_keyboard(focus.id),
        )
    elif action == "blocked":
        await callback.message.edit_text(
            f"Остановил фокус по задаче «{escape(task.title)}».\n\n"
            "Напиши одним сообщением, что конкретно мешает. Я не буду выдумывать причину."
        )
    else:
        await callback.message.edit_text(f"Готово: {escape(task.title)}.")
    miro_sync_coordinator.schedule(callback.from_user.id)


async def _automations_payload(session, user_id: int, now: datetime):
    reminder_result = await session.execute(
        select(Reminder).where(Reminder.user_id == user_id, Reminder.status.in_(["active", "paused"]))
        .order_by(Reminder.remind_at.asc())
    )
    workout_result = await session.execute(
        select(WorkoutSession).where(
            WorkoutSession.user_id == user_id,
            WorkoutSession.status == "planned",
            WorkoutSession.scheduled_for >= now,
        ).order_by(WorkoutSession.scheduled_for.asc())
    )
    nudge_result = await session.execute(
        select(HealthNudge).where(HealthNudge.user_id == user_id, HealthNudge.date == now.date())
    )
    state = await get_or_create_runtime_state(session, user_id)
    return (
        list(reminder_result.scalars()), list(workout_result.scalars()),
        list(nudge_result.scalars()), state,
    )


async def _automations_text(session, user_id: int, now: datetime):
    reminders, workouts, _nudges, state = await _automations_payload(session, user_id, now)
    lines = ["Автоматизации", "", "Напоминания:"]
    if reminders:
        for reminder in reminders[:10]:
            state = "пауза" if reminder.status == "paused" else "активно"
            lines.append(f"• {escape(reminder.text)} · {ensure_aware(reminder.remind_at, now.tzinfo).strftime('%d.%m %H:%M')} · {state}")
    else:
        lines.append("• нет")
    lines += ["", "Тренировки:"]
    lines.extend(
        f"• {ensure_aware(item.scheduled_for, now.tzinfo).strftime('%d.%m %H:%M')} · {escape(item.title)}"
        for item in workouts[:5]
    )
    if not workouts:
        lines.append("• нет")
    lines += [
        "",
        f"Контекстная подсказка по задачам: {'включена' if state.checkin_enabled else 'выключена'}.",
    ]
    rows = []
    for reminder in reminders[:5]:
        if reminder.status == "active":
            rows.append([
                InlineKeyboardButton(text=f"Пауза: {reminder.text[:20]}", callback_data=f"automation_pause:{reminder.id}"),
                InlineKeyboardButton(text="Удалить", callback_data=f"automation_cancel:{reminder.id}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(text=f"Возобновить: {reminder.text[:16]}", callback_data=f"automation_resume:{reminder.id}")
            ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.message(Command("automations"))
async def automations(message: Message, session_factory, time_service, screen_service):
    async with session_factory() as session:
        text, keyboard = await _automations_text(session, message.from_user.id, time_service.now())
    await message.answer(text, reply_markup=keyboard)
    await screen_service.delete_user_input(message)


@router.callback_query(F.data == "nav_automations")
async def nav_automations(callback: CallbackQuery, session_factory, time_service):
    async with session_factory() as session:
        text, keyboard = await _automations_text(session, callback.from_user.id, time_service.now())
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=keyboard)


async def _change_reminder(callback, session_factory, reminder_scheduler, time_service, action):
    reminder_id = int(callback.data.rsplit(":", 1)[1])
    async with session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        if not reminder or reminder.user_id != callback.from_user.id:
            await callback.answer("Напоминание не найдено", show_alert=True)
            return
        if action == "pause":
            reminder.status = "paused"
            reminder_scheduler.cancel_reminder_job(reminder.id)
        elif action == "cancel":
            reminder.status = "cancelled"
            reminder_scheduler.cancel_reminder_job(reminder.id)
        else:
            reminder.status = "active"
            if ensure_aware(reminder.remind_at, time_service.now().tzinfo) <= time_service.now():
                reminder.remind_at = time_service.now() + timedelta(minutes=15)
            await session.flush()
            reminder_scheduler.schedule_reminder(reminder)
        await session.commit()
        text, keyboard = await _automations_text(session, callback.from_user.id, time_service.now())
    await callback.answer("Готово")
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("automation_pause:"))
async def automation_pause(callback, session_factory, reminder_scheduler, time_service):
    await _change_reminder(callback, session_factory, reminder_scheduler, time_service, "pause")


@router.callback_query(F.data.startswith("automation_resume:"))
async def automation_resume(callback, session_factory, reminder_scheduler, time_service):
    await _change_reminder(callback, session_factory, reminder_scheduler, time_service, "resume")


@router.callback_query(F.data.startswith("automation_cancel:"))
async def automation_cancel(callback, session_factory, reminder_scheduler, time_service):
    await _change_reminder(callback, session_factory, reminder_scheduler, time_service, "cancel")


async def _projects_text(session, user_id: int) -> str:
    result = await session.execute(
        select(Task).where(Task.user_id == user_id, Task.project != "")
        .order_by(Task.project.asc(), Task.status.asc(), Task.id.asc())
    )
    tasks = list(result.scalars().all())
    obstacles_result = await session.execute(
        select(ProblemBlock).where(
            ProblemBlock.user_id == user_id, ProblemBlock.status == "active"
        ).order_by(ProblemBlock.updated_at.desc()).limit(5)
    )
    obstacles = list(obstacles_result.scalars().all())
    grouped = defaultdict(list)
    for task in tasks:
        grouped[task.project].append(task)
    if not grouped and not obstacles:
        return "Проектов пока нет. Напиши: «разбей [цель] на конкретные шаги»."
    else:
        lines = ["Проекты"]
        for project, items in list(grouped.items())[:10]:
            active = [item for item in items if item.status == "active"]
            done = [item for item in items if item.status in {"done", "archived"}]
            blocked = [item for item in active if item.workflow_state == "blocked"]
            next_task = next((item for item in active if item.workflow_state != "blocked"), None)
            lines += ["", escape(project), f"Готово: {len(done)} · осталось: {len(active)} · препятствий: {len(blocked)}"]
            if next_task:
                lines.append(f"Следующее действие: {escape(next_task.next_action or next_task.title)}")
        if obstacles:
            lines += ["", "Препятствия, которые требуют решения:"]
            for obstacle in obstacles:
                lines.append(f"• {escape(obstacle.title)}")
                lines.append(f"  Следующее действие: {escape(obstacle.next_action or 'уточнить вместе')}")
        return "\n".join(lines)


@router.message(Command("projects"))
async def projects(message: Message, session_factory, screen_service, project_service, time_service):
    async with session_factory() as session:
        text = await project_service.render_projects(session, message.from_user.id, time_service.now())
        await session.commit()
    await message.answer(text)
    await screen_service.delete_user_input(message)


@router.callback_query(F.data == "nav_projects")
async def nav_projects(callback: CallbackQuery, session_factory, project_service, time_service):
    async with session_factory() as session:
        text = await project_service.render_projects(session, callback.from_user.id, time_service.now())
        await session.commit()
    await callback.answer()
    await callback.message.edit_text(text)


@router.message(Command("review"))
async def weekly_review(message: Message, session_factory, screen_service, project_service, time_service):
    async with session_factory() as session:
        text = await project_service.weekly_review(session, message.from_user.id, time_service.now())
        await session.commit()
    await message.answer(text)
    await screen_service.delete_user_input(message)


@router.message(Command("calendar"))
async def calendar_status(message: Message, session_factory, calendar_service, time_service, screen_service):
    async with session_factory() as session:
        plan = await calendar_service.build_day_plan(session, message.from_user.id, time_service.now())
        await session.commit()
    lines = ["Календарь и загрузка на сегодня", ""]
    if plan["events"]:
        lines.append(f"Занятых событий: {len(plan['events'])}.")
    else:
        lines.append("Событий нет. Для синхронизации отправь календарь через iPhone Shortcut bridge.")
    lines.append(f"Запланировано задач: {len(plan['timeboxes'])}.")
    lines.append(f"Не помещается: {len(plan['unscheduled'])}.")
    lines.append(f"Входящие без данных: {len(plan['inbox'])}.")
    await message.answer("\n".join(lines))
    await screen_service.delete_user_input(message)


@router.message(Command("metrics"))
async def metrics(message: Message, session_factory, metric_service, time_service, screen_service):
    async with session_factory() as session:
        data = await metric_service.summary(session, message.from_user.id, time_service.now())
    events = data["events"]
    previews = int(events.get("preview_created", 0))
    accepted = int(events.get("preview_accepted", 0))
    edited = int(events.get("preview_edited", 0))
    clarification = int(events.get("clarification_requested", 0))
    acceptance = round(accepted / previews * 100) if previews else 0
    total_tokens = data["input_tokens"] + data["output_tokens"]
    text = (
        f"Качество ассистента · {data['days']} дней\n\n"
        f"Проверок действий: {previews}\n"
        f"Подтверждено: {accepted} ({acceptance}%)\n"
        f"Исправлено перед сохранением: {edited}\n"
        f"Потребовалось уточнение: {clarification}\n"
        f"Fallback-разборов: {data['parser_fallbacks']}\n"
        f"Голосовых запросов: {data['voice_requests']}\n"
        f"P95 разбора: {data['p95_latency_ms']} мс\n"
        f"Оценено результатов: {data['previews_reviewed']}\n"
        f"Исправлений после результата: {data['preview_errors']} "
        f"({data['preview_error_rate']:.0%})\n"
        f"Уточнения завершены: {data['clarification_resolved']} из "
        f"{data['clarification_requests']} "
        f"({data['clarification_resolution_rate']:.0%})\n"
        f"Фокус-сессий начато: {events.get('focus_started', 0)}\n"
        f"Напоминаний доставлено: {data['reminders_delivered']}\n"
        f"P95 задержки напоминаний: {data['reminder_delivery_p95_seconds']} сек.\n"
        f"Неудачных доставок: {data['reminder_deliveries_failed']}\n"
        f"Действий с напоминаниями: {data['reminder_actions']}\n"
        f"Токены OpenAI: {total_tokens} (вход {data['input_tokens']}, выход {data['output_tokens']})"
    )
    await message.answer(text)
    await screen_service.delete_user_input(message)
