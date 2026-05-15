from __future__ import annotations

import json
import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — личный AI-ассистент пользователя. Верни только JSON формата {\"intents\": [...]} без пояснений."""


class Intent(BaseModel):
    type: Literal["create_task", "create_reminder", "schedule_override", "rest_day", "do_nothing"]
    text: str | None = None
    title: str | None = None
    description: str | None = None
    date: str | None = None
    time: str | None = None
    priority: str | None = "medium"
    mode: str | None = None
    create_tasks: bool | None = True
    write_to_miro: bool | None = False
    is_minor: bool | None = False
    auto_cleanup_allowed: bool | None = False


class AIResult(BaseModel):
    intents: list[Intent] = Field(default_factory=list)


class AIService:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    async def parse_intents(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            return self._fallback(text)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"text": text, "context": context}, ensure_ascii=False)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post("https://api.openai.com/v1/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                parsed = AIResult.model_validate(json.loads(content))
                return parsed.model_dump()
        except Exception as exc:
            logger.exception("OpenAI parse failed: %s", exc)
            return self._fallback(text)

    def _fallback(self, text: str) -> dict[str, Any]:
        lowered = text.lower()
        if "не запис" in lowered or "просто подумай" in lowered:
            return {"intents": [{"type": "do_nothing"}]}
        intents: list[dict[str, Any]] = []
        if "отдыха" in lowered and ("завтра" in lowered or "today" in lowered):
            intents.append({"type": "rest_day", "date": "tomorrow" if "завтра" in lowered else "today", "mode": "rest_day", "create_tasks": False, "write_to_miro": False})
        if "напомни" in lowered:
            intents.append({"type": "create_reminder", "text": text, "date": "tomorrow" if "завтра" in lowered else "today", "time": "evening" if "веч" in lowered else "day", "priority": "medium"})
        if not intents:
            intents.append({"type": "create_task", "title": text, "priority": "medium"})
        return {"intents": intents}
