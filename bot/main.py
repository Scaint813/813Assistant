from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import get_config
from bot.database.migrations import create_schema
from bot.handlers import domains, menu, quick_note, reminders, start, voice
from bot.handlers import health as health_handler
from bot.middlewares import AccessMiddleware
from bot.services.ai_service import AIService
from bot.services.cleanup_service import CleanupService
from bot.services.checkin_service import CheckinService
from bot.services.intent_parser import IntentParser
from bot.services.miro_service import MiroService
from bot.services.navigation_service import NavigationService
from bot.services.next_step_service import NextStepService
from bot.services.overload_service import OverloadService
from bot.services.problem_block_service import ProblemBlockService
from bot.services.problem_resources_service import ProblemResourcesService
from bot.services.reminder_scheduler import ReminderScheduler
from bot.services.screen_service import ScreenService
from bot.services.time_service import TimeService
from bot.services.transcription_service import TranscriptionService


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    # ── 1. Config ──────────────────────────────────────────────────────────
    cfg = get_config()
    log.info("Config loaded. Bot ID=%s, user=%s", cfg.bot_id, cfg.allowed_user_id)

    # ── 2. DB / schema ─────────────────────────────────────────────────────
    engine = create_async_engine(cfg.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await create_schema(engine)
    log.info("Database ready: %s", cfg.database_url)

    # ── 3. Bot & Dispatcher ────────────────────────────────────────────────
    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # ── 4. Middlewares ─────────────────────────────────────────────────────
    dp.message.middleware(AccessMiddleware(cfg.allowed_user_id))
    dp.callback_query.middleware(AccessMiddleware(cfg.allowed_user_id))

    # ── 5. Handlers ────────────────────────────────────────────────────────
    dp.include_router(start.router)
    dp.include_router(health_handler.router)
    dp.include_router(reminders.router)
    dp.include_router(menu.router)
    dp.include_router(domains.router)
    dp.include_router(voice.router)
    dp.include_router(quick_note.router)

    # ── 6. Services ────────────────────────────────────────────────────────
    time_service = TimeService(cfg.timezone, cfg.morning_time, cfg.day_time, cfg.evening_time, cfg.night_time)
    ai_service = AIService(cfg.openai_api_key, cfg.openai_model_fast, cfg.openai_model_smart, cfg.openai_model)
    intent_parser = IntentParser(ai_service, time_service)
    transcription_service = TranscriptionService(cfg.openai_api_key, cfg.transcription_model)
    miro_service = MiroService(cfg.miro_token, cfg.miro_board_id, cfg.miro_ai_zone_start_x, cfg.miro_ai_zone_start_y)
    problem_block_service = ProblemBlockService()
    problem_resources_service = ProblemResourcesService()
    next_step_service = NextStepService()
    overload_service = OverloadService()

    # ── 7. Scheduler ───────────────────────────────────────────────────────
    reminder_scheduler = ReminderScheduler(cfg.timezone, bot, session_factory)
    await reminder_scheduler.start_scheduler()
    log.info("APScheduler started")

    checkin_service = CheckinService(
        reminder_scheduler.scheduler,
        session_factory,
        time_service,
        problem_block_service,
        next_step_service,
        overload_service,
        cfg.allowed_user_id,
        cfg.checkin_enabled,
        cfg.checkin_morning_time,
        cfg.checkin_day_time,
        cfg.checkin_evening_time,
    )
    checkin_service.schedule_daily_checkins(bot)
    log.info(
        "Check-ins: %s | morning=%s day=%s evening=%s",
        "enabled" if cfg.checkin_enabled else "disabled",
        cfg.checkin_morning_time,
        cfg.checkin_day_time,
        cfg.checkin_evening_time,
    )

    # ── 8. DI wiring ───────────────────────────────────────────────────────
    dp["config"] = cfg
    dp["session_factory"] = session_factory
    dp["intent_parser"] = intent_parser
    dp["transcription_service"] = transcription_service
    dp["cleanup_service"] = CleanupService()
    dp["time_service"] = time_service
    dp["miro_service"] = miro_service
    dp["reminder_scheduler"] = reminder_scheduler
    dp["navigation_service"] = NavigationService()
    dp["overload_service"] = overload_service
    dp["problem_block_service"] = problem_block_service
    dp["problem_resources_service"] = problem_resources_service
    dp["next_step_service"] = next_step_service
    dp["checkin_service"] = checkin_service
    dp["screen_service"] = ScreenService()

    log.info(
        "Services: OpenAI=%s | Miro=%s | Transcription=%s",
        "set" if cfg.openai_api_key else "missing (fallback parser)",
        "set" if miro_service.is_configured() else "missing (sync disabled)",
        "set" if cfg.openai_api_key else "missing",
    )
    log.info("813Assistant started. Polling...")

    # ── 9. Polling ─────────────────────────────────────────────────────────
    try:
        await dp.start_polling(bot)
    finally:
        reminder_scheduler.shutdown_scheduler()
        log.info("Scheduler stopped. Bot shutdown.")


if __name__ == "__main__":
    asyncio.run(main())
