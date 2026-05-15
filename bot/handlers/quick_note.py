from aiogram import F, Router
from aiogram.types import Message

from bot.database.queries import create_pending_preview
from bot.keyboards.inline import confirm_keyboard
from bot.services.action_preview import render_preview

router = Router()


@router.message(F.text & ~F.text.startswith("/"))
async def capture_text(message: Message, intent_parser, session_factory):
    parsed = await intent_parser.parse(message.text, context={})
    async with session_factory() as session:
        preview = await create_pending_preview(session, message.from_user.id, "text", message.text, message.text, parsed)
        await session.commit()
    await message.answer(f"Расшифровка:\n{parsed['transcript']}\n\n{render_preview(parsed)}", reply_markup=confirm_keyboard(preview.id))
