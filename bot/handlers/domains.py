from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("quick"))
async def quick(message: Message):
    await message.answer("Отправьте текст одной строкой — я подготовлю Action Preview.")


@router.message(Command("rest"))
async def rest(message: Message):
    await message.answer("Режим rest day (MVP): отправьте текстом 'завтра отдыхаю' для подтверждаемого действия.")


@router.message(Command("finance"))
@router.message(Command("workout"))
@router.message(Command("study"))
@router.message(Command("goals"))
@router.message(Command("sync_miro"))
@router.message(Command("rebuild_miro"))
@router.message(Command("archive"))
@router.message(Command("cleanup"))
@router.message(Command("settings"))
async def stubs(message: Message):
    await message.answer("Раздел в MVP в процессе: интерфейс подключен, бизнес-логика будет расширена.")
