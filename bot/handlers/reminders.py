from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("reminders"))
async def reminders(message: Message):
    await message.answer("Список напоминаний будет здесь (MVP).")
