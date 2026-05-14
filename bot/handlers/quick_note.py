from aiogram import F, Router
from aiogram.types import Message

from bot.keyboards.inline import confirm_keyboard
from bot.database.queries import save_pending_preview
from bot.services.action_preview import render_preview

router = Router()


@router.message(F.text)
async def capture_text(message: Message, ai_service, session_factory):
    parsed = await ai_service.parse_intents(message.text, context={})
    async with session_factory() as session:
        await save_pending_preview(session, message.from_user.id, message.text, parsed)
        await session.commit()
    preview = render_preview(parsed)
    await message.answer(f"Расшифровка:\n{parsed['transcript']}\n\n{preview}", reply_markup=confirm_keyboard())
