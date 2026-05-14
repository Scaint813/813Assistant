from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot.keyboards.main_menu import main_menu

router = Router()


@router.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer("Привет! Я твой AI-ассистент. Выбери действие в меню.", reply_markup=main_menu())
