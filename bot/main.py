from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import get_config
from bot.database.migrations import create_schema
from bot.handlers import preview, quick_note, reminders, start, today, voice
from bot.middlewares import PrivateUserOnlyMiddleware
from bot.services.ai_service import AIService
from bot.services.time_service import TimeService
from bot.services.transcription_service import TranscriptionService


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = get_config()
    engine = create_async_engine(cfg.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await create_schema(engine)

    dp = Dispatcher()
    dp.message.middleware(PrivateUserOnlyMiddleware(cfg.allowed_user_id))
    dp.include_router(start.router)
    dp.include_router(reminders.router)
    dp.include_router(today.router)
    dp.include_router(preview.router)
    dp.include_router(voice.router)
    dp.include_router(quick_note.router)

    ai_service = AIService(cfg.openai_model)
    time_service = TimeService(cfg.timezone, defaults={"morning": "09:00", "day": "14:00", "evening": "19:00", "night": "22:00"})
    transcription_service = TranscriptionService()

    dp["ai_service"] = ai_service
    dp["transcription_service"] = transcription_service
    dp["time_service"] = time_service
    dp["session_factory"] = session_factory
    dp["cfg"] = cfg

    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    if cfg.bot_id and me.id != cfg.bot_id:
        raise RuntimeError(f"BOT_ID mismatch: expected {cfg.bot_id}, got {me.id}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
