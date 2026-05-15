from __future__ import annotations

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class TranscriptionService:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model or "whisper-1"

    async def transcribe_audio(self, file_path: str) -> str | None:
        if not self.api_key:
            logger.warning("Transcription unavailable: OPENAI_API_KEY is missing")
            return None
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(file_path, "rb") as f:
                    files = {"file": (Path(file_path).name, f, "audio/ogg")}
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
        except Exception as exc:
            logger.exception("Transcription failed: %s", exc)
            return None
