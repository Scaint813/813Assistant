from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot.database.queries import get_or_create_user_profile
from bot.keyboards.main_menu import main_menu

router = Router()


@router.message(CommandStart())
async def start_cmd(message: Message, session_factory, config, navigation_service):
    async with session_factory() as session:
        await get_or_create_user_profile(
            session,
            message.from_user.id,
            message.from_user.full_name or "",
            str(config.timezone),
        )
        await session.commit()
    navigation_service.reset(message.from_user.id)
    # ReplyKeyboard only — no inline buttons on the start screen
    await message.answer(
        "813Assistant\n\nШтаб открыт.\nВыбери блок или напиши задачу обычным текстом.",
        reply_markup=main_menu(),
    )
