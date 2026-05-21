from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.database.queries import (
    get_active_problem_blocks,
    get_active_reminders,
    get_active_tasks,
    get_all_exam_dates,
    get_all_study_schedule,
    get_archived_tasks,
    get_or_create_runtime_state,
    get_upcoming_overrides,
)

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("health"))
async def health_cmd(message: Message, session_factory, time_service, miro_service, reminder_scheduler, config, checkin_service, screen_service):
    """Quick smoke-test. Safe: never shows tokens."""
    now = time_service.now()

    # DB check
    db_ok = True
    db_tasks = 0
    db_reminders = 0
    db_blocks = 0
    try:
        async with session_factory() as session:
            db_tasks = len(await get_active_tasks(session, message.from_user.id))
            db_reminders = len(await get_active_reminders(session, message.from_user.id))
            db_blocks = len(await get_active_problem_blocks(session, message.from_user.id, now))
            state = await get_or_create_runtime_state(session, message.from_user.id)
            await session.commit()
    except Exception as exc:
        logger.exception("Health check DB error: %s", exc)
        db_ok = False

    # Scheduler check
    scheduler_running = reminder_scheduler.scheduler.running

    # Pending scheduled jobs for this user
    try:
        reminder_jobs = sum(
            1 for job in reminder_scheduler.scheduler.get_jobs()
            if job.id.startswith("reminder:")
        )
    except Exception:
        reminder_jobs = -1

    # Config flags (no token values)
    openai_status = "set" if config.openai_api_key else "missing (fallback parser active)"
    miro_status = "set" if miro_service.is_configured() else "missing (sync disabled)"
    checkin_status = "enabled" if config.checkin_enabled else "disabled"

    # Quiet mode
    quiet_info = ""
    if db_ok and state.quiet_until and state.quiet_until > now:
        quiet_info = f"\nQuiet until: {state.quiet_until.strftime('%H:%M %d.%m')}"

    text = (
        "813Assistant health\n\n"
        f"DB: {'OK' if db_ok else 'FAIL'}\n"
        f"OpenAI: {openai_status}\n"
        f"Miro: {miro_status}\n"
        f"Scheduler: {'running' if scheduler_running else 'STOPPED'}\n"
        f"Scheduled reminder jobs: {reminder_jobs}\n"
        f"Check-ins: {checkin_status}\n"
        f"Timezone: {config.timezone}\n"
        f"Now: {now.strftime('%Y-%m-%d %H:%M %Z')}\n\n"
        f"Active tasks: {db_tasks}\n"
        f"Active reminders: {db_reminders}\n"
        f"Active problem blocks: {db_blocks}"
        f"{quiet_info}"
    )
    await message.answer(text)
    await screen_service.delete_user_input(message)


@router.message(Command("debug_create_test_data"))
async def debug_create_test_data(message: Message, session_factory, time_service, reminder_scheduler, screen_service):
    """
    DEBUG ONLY. Creates 1 task + 1 reminder (+1 min) + 1 problem block.
    Accessible only to ALLOWED_USER_ID (enforced by AccessMiddleware).
    Not shown in main menu.
    """
    from datetime import timedelta
    from bot.database.queries import create_task, create_reminder, create_problem_block

    now = time_service.now()
    remind_at = now + timedelta(minutes=1)

    async with session_factory() as session:
        task = await create_task(
            session,
            message.from_user.id,
            "[DEBUG] Test task",
            description="Создана командой /debug_create_test_data",
            priority="medium",
            is_minor=True,
            auto_cleanup_allowed=True,
        )
        reminder = await create_reminder(
            session,
            message.from_user.id,
            "[DEBUG] Test reminder — через 1 минуту",
            remind_at=remind_at,
            priority="medium",
        )
        block = await create_problem_block(
            session,
            message.from_user.id,
            category="other",
            title="[DEBUG] Test problem block",
            problem_text="Тестовый блок для проверки /problems",
            solution_strategy="Шаг 1. Шаг 2. Шаг 3",
            next_action="Проверить что блок виден в /problems",
            status="active",
            priority="low",
            pressure_level="normal",
            source_type="debug",
            resources_json="[]",
        )
        await session.commit()

    reminder_scheduler.schedule_reminder(reminder)

    await message.answer(
        "[DEBUG] Тестовые данные созданы:\n"
        f"- Задача ID={task.id}: {task.title}\n"
        f"- Напоминание ID={reminder.id}: сработает в {remind_at.strftime('%H:%M:%S')}\n"
        f"- Блок проблем ID={block.id}: {block.title}\n\n"
        "Проверь: /tasks /reminders /problems\n"
        "Через ~1 мин придёт тестовое напоминание."
    )
    await screen_service.delete_user_input(message)


@router.message(Command("miro_debug"))
async def miro_debug_cmd(message: Message, miro_service, config, screen_service):
    """
    Miro connectivity diagnostic.
    Safe: never shows MIRO_ACCESS_TOKEN or BOT_TOKEN.
    Only accessible to ALLOWED_USER_ID via AccessMiddleware.
    """
    if not miro_service.is_configured():
        await message.answer(
            "MIRO DEBUG\n\n"
            "Статус: не настроен.\n\n"
            "Нужны переменные окружения:\n"
            "  MIRO_ACCESS_TOKEN\n"
            "  MIRO_BOARD_ID"
        )
        await screen_service.delete_user_input(message)
        return

    board_display = config.miro_board_id[:8] + "..." if config.miro_board_id else "—"
    lines = [
        "MIRO DEBUG",
        "",
        f"Board: {board_display}",
        f"AI-зона: X={config.miro_ai_zone_start_x}, Y={config.miro_ai_zone_start_y}",
        "",
    ]

    # GET /v2/boards/{id}/items?limit=1
    get_result = await miro_service.debug_get_items()
    get_status = get_result["status_code"]
    if get_status == 200:
        lines.append(f"GET items: {get_status} OK")
    elif get_status is None:
        lines.append(f"GET items: сетевая ошибка — {get_result['error']}")
    else:
        lines.append(f"GET items: {get_status} FAIL — {get_result['error']}")

    # POST — create test sticky note
    create_result = await miro_service.debug_create_test_note()
    create_status = create_result["status_code"]
    if create_status == 201:
        lines += [f"CREATE test note: {create_status} OK", f"item_id: {create_result['item_id']}"]
    elif create_status is None:
        lines.append(f"CREATE test note: сетевая ошибка — {create_result['error']}")
    else:
        lines += [
            f"CREATE test note: {create_status} FAIL",
            f"Error: {create_result['error']}",
        ]

    lines += [
        "",
        "Логи: journalctl -u 813assistant -n 120 --no-pager",
    ]
    await message.answer("\n".join(lines))
    await screen_service.delete_user_input(message)


@router.message(Command("debug_db_state"))
async def debug_db_state_cmd(message: Message, session_factory, time_service, config, screen_service):
    """
    Debug command: show raw DB state for ALLOWED_USER_ID only.
    Safe: never shows tokens/passwords.
    """
    if message.from_user.id != config.allowed_user_id:
        return
    now = time_service.now()
    lines = ["/debug_db_state", ""]

    try:
        async with session_factory() as session:
            tasks = await get_active_tasks(session, message.from_user.id)
            archived = await get_archived_tasks(session, message.from_user.id)
            reminders = await get_active_reminders(session, message.from_user.id)
            blocks = await get_active_problem_blocks(session, message.from_user.id, now)
            exams = await get_all_exam_dates(session, message.from_user.id)
            schedule = await get_all_study_schedule(session, message.from_user.id)
            overrides = await get_upcoming_overrides(session, message.from_user.id, now.date())
            await session.commit()
    except Exception as exc:
        await message.answer(f"DB error: {exc}")
        return

    # Tasks
    lines.append(f"TASKS (active: {len(tasks)}, archived: {len(archived)}):")
    for t in tasks[:10]:
        dl = t.deadline.strftime("%Y-%m-%d") if t.deadline else "—"
        lines.append(f"  [{t.id}] {t.title[:50]} | cat={t.category} | pr={t.priority} | dl={dl} | st={t.status}")
    if not tasks:
        lines.append("  (none)")
    lines.append("")

    # Reminders
    lines.append(f"REMINDERS (active: {len(reminders)}):")
    for r in reminders[:10]:
        lines.append(f"  [{r.id}] {r.text[:50]} | at={r.remind_at.strftime('%Y-%m-%d %H:%M')} | st={r.status}")
    if not reminders:
        lines.append("  (none)")
    lines.append("")

    # Problem blocks
    lines.append(f"PROBLEM BLOCKS (active: {len(blocks)}):")
    for b in blocks[:10]:
        dl = b.deadline.strftime("%Y-%m-%d") if b.deadline else "—"
        lines.append(f"  [{b.id}] {b.title[:50]} | cat={b.category} | pr={b.priority} | dl={dl}")
        lines.append(f"      next: {b.next_action[:50]}")
    if not blocks:
        lines.append("  (none)")
    lines.append("")

    # Exam dates
    lines.append(f"EXAM DATES ({len(exams)}):")
    for e in exams:
        lines.append(f"  [{e.id}] {e.subject} | {e.exam_date} | st={e.status}")
    if not exams:
        lines.append("  (none)")
    lines.append("")

    # Study schedule
    lines.append(f"STUDY SCHEDULE ({len(schedule)}):")
    for s in schedule:
        lines.append(f"  [{s.id}] {s.subject} | {s.weekday} {s.time_str} | tutor={s.tutor_name} | st={s.status}")
    if not schedule:
        lines.append("  (none)")
    lines.append("")

    # Schedule overrides
    lines.append(f"SCHEDULE OVERRIDES ({len(overrides)}):")
    for o in overrides[:5]:
        lines.append(f"  [{o.id}] {o.date} | {o.mode}")
    if not overrides:
        lines.append("  (none)")

    # Send in chunks (Telegram 4096 char limit)
    text = "\n".join(lines)
    for i in range(0, len(text), 3800):
        await message.answer(text[i:i+3800])
    await screen_service.delete_user_input(message)
