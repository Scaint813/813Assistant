from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class TranscriptionService:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    async def transcribe_file(self, path: str) -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY не настроен для транскрибации")
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(path, "rb") as f:
                    files = {"file": ("voice.ogg", f, "audio/ogg")}
                    data = {"model": self.model}
                    r = await client.post("https://api.openai.com/v1/audio/transcriptions", headers={"Authorization": f"Bearer {self.api_key}"}, data=data, files=files)
                r.raise_for_status()
                return r.json().get("text", "").strip()
        except Exception as exc:
            logger.exception("Transcription failed: %s", exc)
            raise RuntimeError("Не удалось расшифровать голосовое сообщение") from exc
