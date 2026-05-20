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
    get_or_create_runtime_state,
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
