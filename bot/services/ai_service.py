from __future__ import annotations

import json
import logging

from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """
Ты — личный AI-ассистент. Возвращай ТОЛЬКО JSON формата {"intents": [...]}.
Разрешённые intents: create_task, create_reminder, rest_day, show_today, show_tasks, do_nothing.
Если пользователь явно просит ничего не сохранять ("не записывай", "просто подумай", "ничего не сохраняй") и отдельно не просит "напомни" — верни do_nothing.
Если просит показать сегодня — show_today. Если просит показать задачи — show_tasks.
Для create_reminder старайся вернуть remind_at ISO datetime с timezone. Если не уверен — верни date/time словами.
""".strip()


class Intent(BaseModel):
    type: Literal["create_task", "create_reminder", "rest_day", "show_today", "show_tasks", "do_nothing"]
    title: str | None = None
    description: str | None = None
    priority: str | None = "medium"
    deadline: str | None = None
    is_minor: bool | None = False
    auto_cleanup_allowed: bool | None = False
    text: str | None = None
    remind_at: str | None = None
    date: str | None = None
    time: str | None = None
    reply: str | None = None
    create_tasks: bool | None = False
    write_to_miro: bool | None = False

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

    async def parse_intent_with_openai(self, text: str, context: dict[str, Any]) -> dict[str, Any] | None:
        if not self.api_key:
            logger.info("OPENAI_API_KEY is not configured; using fallback parser")
            return None

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
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                parsed = AIResult.model_validate(json.loads(content))
                return parsed.model_dump()
        except Exception as exc:
            logger.exception("OpenAI parser failed, fallback will be used: %s", exc)
            return None

    def parse_intent_fallback(self, text: str) -> dict[str, Any]:
        lowered = text.lower().strip()

        if any(p in lowered for p in ("что сегодня", "что у меня сегодня", "покажи сегодня", "план на сегодня")):
            return {"intents": [{"type": "show_today"}]}
        if any(p in lowered for p in ("покажи задачи", "что по задачам", "активные задачи")):
            return {"intents": [{"type": "show_tasks"}]}

        do_nothing_markers = ("не записывай", "просто подумай", "ничего не сохраняй", "не добавляй")
        if any(p in lowered for p in do_nothing_markers) and "напомни" not in lowered:
            return {"intents": [{"type": "do_nothing", "reply": "Понял, ничего не записываю."}]}

        if any(p in lowered for p in ("отдыхаю", "день отдыха", "ничего не ставь", "без тренировки")):
            return {
                "intents": [
                    {
                        "type": "rest_day",
                        "date": "today" if "сегодня" in lowered else ("tomorrow" if "завтра" in lowered else "today"),
                        "title": "День отдыха",
                        "create_tasks": False,
                        "write_to_miro": False,
                    }
                ]
            }

        if "напомни" in lowered:
            cleaned = text
            for marker in ("завтра вечером напомни", "через два дня утром напомни", "через 2 дня утром напомни", "вечером напомни", "завтра напомни", "сегодня напомни", "напомни"):
                idx = lowered.find(marker)
                if idx >= 0:
                    cleaned = text[idx + len(marker) :].strip(" :,-") or text
                    break
            return {"intents": [{"type": "create_reminder", "text": cleaned, "date": "today", "time": "evening", "priority": "medium"}]}

        return {
            "intents": [
                {
                    "type": "create_task",
                    "title": text,
                    "description": "",
                    "priority": "medium",
                    "deadline": None,
                    "is_minor": False,
                    "auto_cleanup_allowed": False,
                }
            ]
        }
