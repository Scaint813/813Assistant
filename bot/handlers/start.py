from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot.database.queries import get_or_create_user_profile
from bot.keyboards.main_menu import main_menu

router = Router()


@router.message(CommandStart())
async def start_cmd(message: Message, session_factory, config):
    async with session_factory() as session:
        await get_or_create_user_profile(session, message.from_user.id, message.from_user.full_name or "", str(config.timezone))
        await session.commit()
    await message.answer("Привет! Я твой AI-ассистент. Отправь текст или голосовую заметку.", reply_markup=main_menu())
