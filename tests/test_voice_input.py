from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, PendingPreview
from bot.handlers.voice import capture_voice
from bot.services.assistant_ux_service import AssistantScreen
from bot.services.transcription_service import TranscriptionService


class FakeMessage:
    def __init__(self, user_id: int = 813):
        self.voice = SimpleNamespace(file_id="voice-file-id")
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, reply_markup=None):
        self.answers.append((text, reply_markup))


class FakeBot:
    async def get_file(self, file_id: str):
        assert file_id == "voice-file-id"
        return SimpleNamespace(file_path="voice/message.ogg")

    async def download_file(self, file_path: str, destination: str):
        assert file_path == "voice/message.ogg"
        Path(destination).write_bytes(b"telegram-ogg-audio")


class FakeTranscriptionService:
    api_key = "configured"

    def __init__(self, transcript: str):
        self.transcript = transcript
        self.temp_path: Path | None = None

    async def transcribe_audio(self, file_path: str):
        self.temp_path = Path(file_path)
        assert self.temp_path.is_file()
        return self.transcript


class FakeIntentParser:
    def __init__(self, parsed: dict):
        self.parsed = parsed
        self.received_text = ""

    async def parse_user_text(self, text: str, context: dict):
        self.received_text = text
        assert context["user_id"] == 813
        return self.parsed


class FakeScreenService:
    def __init__(self):
        self.deleted = False

    async def delete_user_input(self, message):
        self.deleted = True


class FakeTimeService:
    def now(self):
        return datetime(2026, 8, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))


class FakeAssistantUXService:
    def __init__(self):
        self.today_user_id: int | None = None

    async def today(self, session, user_id: int, now: datetime):
        self.today_user_id = user_id
        return AssistantScreen("План с реальными задачами и напоминаниями")

    async def tasks(self, session, user_id: int, now: datetime):
        return AssistantScreen("Задачи с реальными приоритетами")


class VoiceInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_voice_reminder_keeps_transcript_time_and_preview(self):
        transcript = "Напомни сегодня в 18.20 сходить к врачу."
        parsed = {
            "intents": [
                {
                    "type": "create_reminder",
                    "text": "сходить к врачу",
                    "remind_at": "2026-08-01T18:20:00+03:00",
                    "recurrence": "none",
                }
            ]
        }
        message = FakeMessage()
        transcription = FakeTranscriptionService(transcript)
        parser = FakeIntentParser(parsed)
        screen_service = FakeScreenService()

        await capture_voice(
            message=message,
            bot=FakeBot(),
            transcription_service=transcription,
            intent_parser=parser,
            session_factory=self.sessions,
            time_service=FakeTimeService(),
            screen_service=screen_service,
            assistant_ux_service=FakeAssistantUXService(),
        )

        self.assertEqual(transcript, parser.received_text)
        self.assertTrue(screen_service.deleted)
        self.assertIsNotNone(transcription.temp_path)
        self.assertFalse(transcription.temp_path.exists())
        self.assertEqual(1, len(message.answers))
        answer, keyboard = message.answers[0]
        self.assertIn("Расшифровал так", answer)
        self.assertIn("сходить к врачу", answer)
        self.assertIn("18:20", answer)
        self.assertIsNotNone(keyboard)

        async with self.sessions() as session:
            preview = (await session.execute(select(PendingPreview))).scalar_one()
        self.assertEqual("voice", preview.source_type)
        self.assertEqual(transcript, preview.original_text)
        self.assertEqual(transcript, preview.transcript)

    async def test_voice_show_today_uses_normal_data_driven_screen(self):
        message = FakeMessage()
        assistant = FakeAssistantUXService()

        await capture_voice(
            message=message,
            bot=FakeBot(),
            transcription_service=FakeTranscriptionService("Покажи план на сегодня"),
            intent_parser=FakeIntentParser({"intents": [{"type": "show_today"}]}),
            session_factory=self.sessions,
            time_service=FakeTimeService(),
            screen_service=FakeScreenService(),
            assistant_ux_service=assistant,
        )

        self.assertEqual(813, assistant.today_user_id)
        self.assertIn("План с реальными задачами и напоминаниями", message.answers[0][0])
        async with self.sessions() as session:
            previews = (await session.execute(select(PendingPreview))).scalars().all()
        self.assertEqual([], previews)

    async def test_voice_preview_escapes_telegram_html(self):
        message = FakeMessage()
        transcript = "Напомни про <анализы>"
        parsed = {
            "intents": [{
                "type": "create_reminder",
                "text": "проверить <анализы>",
                "remind_at": "2026-08-01T18:20:00+03:00",
            }]
        }

        await capture_voice(
            message=message,
            bot=FakeBot(),
            transcription_service=FakeTranscriptionService(transcript),
            intent_parser=FakeIntentParser(parsed),
            session_factory=self.sessions,
            time_service=FakeTimeService(),
            screen_service=FakeScreenService(),
            assistant_ux_service=FakeAssistantUXService(),
        )

        answer = message.answers[0][0]
        self.assertNotIn("<анализы>", answer)
        self.assertIn("&lt;анализы&gt;", answer)

    async def test_transcription_sends_detected_audio_type(self):
        captured: dict = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"text": "Тестовая расшифровка"}

        class FakeHTTPClient:
            def __init__(self, timeout: float):
                captured["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url: str, *, headers: dict, data: dict, files: dict):
                captured["url"] = url
                captured["model"] = data["model"]
                captured["filename"] = files["file"][0]
                captured["mime_type"] = files["file"][2]
                return FakeResponse()

        audio_path = Path(self.id().replace(".", "-") + ".m4a")
        audio_path.write_bytes(b"m4a-audio")
        try:
            service = TranscriptionService("test-key", "whisper-1")
            with patch("bot.services.transcription_service.httpx.AsyncClient", FakeHTTPClient):
                result = await service.transcribe_audio(str(audio_path))
        finally:
            audio_path.unlink(missing_ok=True)

        self.assertEqual("Тестовая расшифровка", result)
        self.assertEqual("audio/mp4", captured["mime_type"])
        self.assertEqual("whisper-1", captured["model"])
        self.assertEqual(audio_path.name, captured["filename"])

    async def test_transcription_rejects_missing_file_without_http_call(self):
        service = TranscriptionService("test-key", "whisper-1")
        with patch("bot.services.transcription_service.httpx.AsyncClient") as client:
            result = await service.transcribe_audio("definitely-missing-voice.ogg")
        self.assertIsNone(result)
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
