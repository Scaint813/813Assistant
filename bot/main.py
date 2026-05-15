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
from bot.services.ai_service import AIService
from bot.services.transcription_service import TranscriptionService


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = get_config()
    engine = create_async_engine(cfg.database_url)
    async_sessionmaker(engine, expire_on_commit=False)
    await create_schema(engine)

    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(reminders.router)
    dp.include_router(menu.router)
    dp.include_router(domains.router)
    dp.include_router(voice.router)
    dp.include_router(quick_note.router)

    ai_service = AIService(cfg.openai_model)
    transcription_service = TranscriptionService()

    dp["ai_service"] = ai_service
    dp["transcription_service"] = transcription_service

    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
