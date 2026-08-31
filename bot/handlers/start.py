from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot.database.queries import get_or_create_user_profile
from bot.keyboards.main_menu import main_menu

router = Router()

WELCOME_TEXT = (
    "Пиши или говори как человеку — команды запоминать не нужно.\n\n"
    "Например:\n"
    "• «напомни завтра в 18:20 сходить к врачу»;\n"
    "• «подготовить документы к пятнице, нужно 30 минут»;\n"
    "• «перенеси задачу про документы на завтра»;\n"
    "• «задача с документами готова»;\n"
    "• «разбей запуск проекта на конкретные шаги»;\n"
    "• «составь план на день: что влезет, а что перенести»;\n"
    "• «составь мне план тренировок на три дня в неделю».\n\n"
    "Можно перечислить несколько поручений отдельными строками. "
    "Перед изменением данных я покажу, что именно понял. "
    "Кнопки ниже — примеры фраз, а не отдельные режимы. "
    "В «Профиле» выбери свой город: напоминания и планы используют личный часовой пояс."
)


@router.message(CommandStart())
async def start_cmd(
    message: Message,
    session_factory,
    config,
    navigation_service,
    screen_service,
    bot,
):
    async with session_factory() as session:
        await get_or_create_user_profile(
            session,
            message.from_user.id,
            message.from_user.full_name or "",
            str(config.timezone),
        )
        await session.commit()
    navigation_service.reset(message.from_user.id)
    async with session_factory() as session:
        await screen_service.render_screen(
            bot=bot,
            session=session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=WELCOME_TEXT,
            reply_markup=main_menu(),
        )
        await session.commit()
    await screen_service.delete_user_input(message)
