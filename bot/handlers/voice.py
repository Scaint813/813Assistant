from aiogram import F, Router
from aiogram.types import Message

from bot.keyboards.inline import confirm_keyboard
from bot.services.action_preview import render_preview

router = Router()


@router.message(F.voice)
async def capture_voice(message: Message, bot, transcription_service, ai_service):
    file = await bot.get_file(message.voice.file_id)
    path = f"/tmp/{message.voice.file_id}.ogg"
    await bot.download_file(file.file_path, path)
    transcript = await transcription_service.transcribe_file(path)
    parsed = await ai_service.parse_intents(transcript, context={})
    preview = render_preview(parsed)
    await message.answer(f"Расшифровка:\n{transcript}\n\n{preview}", reply_markup=confirm_keyboard())
