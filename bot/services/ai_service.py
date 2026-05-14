from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """Ты — личный AI-ассистент пользователя. Возвращай только JSON с transcript и intents."""


class AIService:
    def __init__(self, model: str):
        self.model = model

    async def parse_intents(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        # MVP deterministic fallback; replace with OpenAI API call.
        lowered = text.lower()
        intents: list[dict[str, Any]] = []
        if "завтра отдыхаю" in lowered:
            intents.append({"type": "schedule_override", "date": "tomorrow", "mode": "rest_day", "create_tasks": False, "write_to_miro": False})
        if "напомни" in lowered:
            intents.append({"type": "create_reminder", "date": "tomorrow" if "завтра" in lowered else "today", "time": "evening" if "вечер" in lowered else "day", "text": text, "priority": "medium"})
        if not intents:
            intents.append({"type": "do_nothing"})
        return {"transcript": text, "intents": intents}

    @staticmethod
    def to_json(data: dict[str, Any]) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)
