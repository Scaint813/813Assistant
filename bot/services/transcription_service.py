from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import ClassVar

import httpx

logger = logging.getLogger(__name__)


class TranscriptionService:
    MAX_FILE_BYTES = 25 * 1024 * 1024
    MIME_TYPES: ClassVar[dict[str, str]] = {
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".mp4": "audio/mp4",
        ".mpeg": "audio/mpeg",
        ".mpga": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
    }

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model or "whisper-1"

    async def transcribe_audio(self, file_path: str) -> str | None:
        if not self.api_key:
            logger.warning("Transcription unavailable: OPENAI_API_KEY is missing")
            return None
        path = Path(file_path)
        try:
            audio_bytes = await asyncio.to_thread(path.read_bytes)
        except OSError:
            logger.warning("Transcription input cannot be read: %s", file_path)
            return None
        if len(audio_bytes) > self.MAX_FILE_BYTES:
            logger.warning("Transcription input is too large: %s bytes", len(audio_bytes))
            return None
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                mime_type = self.MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
                files = {"file": (path.name, audio_bytes, mime_type)}
                data = {"model": self.model}
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data,
                    files=files,
                )
                response.raise_for_status()
                text = response.json().get("text", "").strip()
                return text or None
        except Exception:
            logger.exception("Transcription failed")
            return None
