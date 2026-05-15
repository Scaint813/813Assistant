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
from bot.middlewares import AccessMiddleware
from bot.services.ai_service import AIService
from bot.services.cleanup_service import CleanupService
from bot.services.intent_parser import IntentParser
from bot.services.miro_service import MiroService
from bot.services.navigation_service import NavigationService
from bot.services.overload_service import OverloadService
from bot.services.problem_block_service import ProblemBlockService
from bot.services.problem_resources_service import ProblemResourcesService
from bot.services.reminder_scheduler import ReminderScheduler
from bot.services.time_service import TimeService
from bot.services.transcription_service import TranscriptionService


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = get_config()
    engine = create_async_engine(cfg.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await create_schema(engine)

    dp = Dispatcher()
    dp.message.middleware(AccessMiddleware(cfg.allowed_user_id))
    dp.callback_query.middleware(AccessMiddleware(cfg.allowed_user_id))

    dp.include_router(start.router)
    dp.include_router(reminders.router)
    dp.include_router(menu.router)
    dp.include_router(domains.router)
    dp.include_router(voice.router)
    dp.include_router(quick_note.router)

    ai_service = AIService(cfg.openai_api_key, cfg.openai_model_fast, cfg.openai_model_smart, cfg.openai_model)
    time_service = TimeService(cfg.timezone, cfg.morning_time, cfg.day_time, cfg.evening_time, cfg.night_time)
    intent_parser = IntentParser(ai_service, time_service)
    transcription_service = TranscriptionService(cfg.openai_api_key, cfg.transcription_model)
    miro_service = MiroService(cfg.miro_token, cfg.miro_board_id, cfg.miro_ai_zone_start_x, cfg.miro_ai_zone_start_y)

    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    reminder_scheduler = ReminderScheduler(cfg.timezone, bot, session_factory)
    await reminder_scheduler.start_scheduler()

    dp["config"] = cfg
    dp["session_factory"] = session_factory
    dp["intent_parser"] = intent_parser
    dp["transcription_service"] = transcription_service
    dp["cleanup_service"] = CleanupService()
    dp["time_service"] = time_service
    dp["miro_service"] = miro_service
    dp["reminder_scheduler"] = reminder_scheduler
    dp["navigation_service"] = NavigationService()
    dp["overload_service"] = OverloadService()
    dp["problem_block_service"] = ProblemBlockService()
    dp["problem_resources_service"] = ProblemResourcesService()
    dp["next_step_service"] = __import__("bot.services.next_step_service", fromlist=["NextStepService"]).NextStepService()

    try:
        await dp.start_polling(bot)
    finally:
        reminder_scheduler.shutdown_scheduler()


if __name__ == "__main__":
    asyncio.run(main())
