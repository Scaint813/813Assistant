from __future__ import annotations

import json
import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
Ты — личный AI-ассистент. Возвращай ТОЛЬКО JSON формата {"intents": [...]}.
Разрешённые intents: create_task, create_reminder, rest_day, show_today, show_tasks, do_nothing.
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


class AIResult(BaseModel):
    intents: list[Intent] = Field(default_factory=list)


class AIService:
    def __init__(self, api_key: str, model_fast: str, model_smart: str, model_default: str = ""):
        self.api_key = api_key
        self.model_fast = model_fast or model_default
        self.model_smart = model_smart or self.model_fast

    def choose_model_for_intent(self, intent_type: str | None, text: str, context: dict[str, Any]) -> str:
        t = (intent_type or "").lower()
        lowered = text.lower()
        smart_markers = ["анализ", "конфликт", "перегруз", "план", "next", "следующий шаг"]
        if t in {"analyze", "overload", "planning", "next_step"} or any(m in lowered for m in smart_markers):
            return self.model_smart or self.model_fast
        return self.model_fast or self.model_smart

    async def parse_intent_with_openai(self, text: str, context: dict[str, Any]) -> dict[str, Any] | None:
        if not self.api_key:
            logger.info("OPENAI_API_KEY is not configured; using fallback parser")
            return None
        model = self.choose_model_for_intent(None, text, context)
        if not model:
            logger.warning("No OpenAI model configured; using fallback parser")
            return None
        logger.info("OpenAI parser model: %s", model)
        payload = {
            "model": model,
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
            logger.exception("OpenAI parser failed, fallback will be used: %s", exc)
            return None

    def parse_intent_fallback(self, text: str) -> dict[str, Any]:
        lowered = text.lower().strip()
        if any(p in lowered for p in ("что сегодня", "что у меня сегодня", "покажи сегодня", "план на сегодня")):
            return {"intents": [{"type": "show_today"}]}
        if any(p in lowered for p in ("покажи задачи", "что по задачам", "активные задачи")):
            return {"intents": [{"type": "show_tasks"}]}
        if any(p in lowered for p in ("не записывай", "просто подумай", "ничего не сохраняй", "не добавляй")) and "напомни" not in lowered:
            return {"intents": [{"type": "do_nothing", "reply": "Понял, ничего не записываю."}]}
        if any(p in lowered for p in ("отдыхаю", "день отдыха", "ничего не ставь", "без тренировки")):
            return {"intents": [{"type": "rest_day", "date": "today" if "сегодня" in lowered else ("tomorrow" if "завтра" in lowered else "today"), "title": "День отдыха", "create_tasks": False, "write_to_miro": False}]}
        if "напомни" in lowered:
            return {"intents": [{"type": "create_reminder", "text": text, "date": "today", "time": "evening", "priority": "medium"}]}
        return {"intents": [{"type": "create_task", "title": text, "description": "", "priority": "medium", "deadline": None, "is_minor": False, "auto_cleanup_allowed": False}]}
