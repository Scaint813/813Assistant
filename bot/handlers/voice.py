from aiogram import F, Router
from aiogram.types import Message

from bot.database.queries import create_pending_preview
from bot.keyboards.inline import confirm_keyboard
from bot.services.action_preview import render_preview

router = Router()


@router.message(F.voice)
async def capture_voice(message: Message, bot, transcription_service, intent_parser, session_factory):
    file = await bot.get_file(message.voice.file_id)
    path = f"/tmp/{message.voice.file_id}.ogg"
    await bot.download_file(file.file_path, path)
    try:
        transcript = await transcription_service.transcribe_file(path)
    except RuntimeError as exc:
        await message.answer(str(exc))
        return
    parsed = await intent_parser.parse(transcript, context={})
    async with session_factory() as session:
        preview = await create_pending_preview(session, message.from_user.id, "voice", transcript, transcript, parsed)
        await session.commit()
    await message.answer(f"Расшифровка:\n{transcript}\n\n{render_preview(parsed)}", reply_markup=confirm_keyboard(preview.id))
