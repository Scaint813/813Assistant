from aiogram import F, Router
from aiogram.types import Message

from bot.keyboards.inline import confirm_keyboard
from bot.services.action_preview import render_preview

router = Router()


@router.message(F.text)
async def capture_text(message: Message, ai_service):
    parsed = await ai_service.parse_intents(message.text, context={})
    preview = render_preview(parsed)
    await message.answer(f"Расшифровка:\n{parsed['transcript']}\n\n{preview}", reply_markup=confirm_keyboard())
