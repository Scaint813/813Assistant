from __future__ import annotations

from typing import Any

from bot.services.time_service import TimeService


class IntentParser:
    def __init__(self, ai_service, time_service: TimeService):
        self.ai_service = ai_service
        self.time_service = time_service

    async def parse_user_text(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        lowered = text.lower().strip()

        if any(p in lowered for p in ("что сегодня", "что у меня сегодня", "покажи сегодня", "план на сегодня")):
            return {"transcript": text, "intents": [{"type": "show_today"}]}

        if any(p in lowered for p in ("покажи задачи", "что по задачам", "активные задачи")):
            return {"transcript": text, "intents": [{"type": "show_tasks"}]}

        if any(p in lowered for p in ("не записывай", "просто подумай", "ничего не сохраняй")):
            return {"transcript": text, "intents": [{"type": "do_nothing"}]}

        if any(p in lowered for p in ("отдыхаю", "день отдыха", "ничего не ставь", "без тренировки")):
            return {
                "transcript": text,
                "intents": [
                    {
                        "type": "rest_day",
                        "mode": "rest_day",
                        "override_date": self.time_service.today().isoformat(),
                        "create_tasks": False,
                        "write_to_miro": False,
                    }
                ],
            }

        if "напомни" in lowered:
            reminder_text = text
            for prefix in ("вечером напомни", "напомни", "завтра напомни", "сегодня напомни"):
                if prefix in lowered:
                    idx = lowered.find(prefix)
                    reminder_text = text[idx + len(prefix):].strip(" :,-") or text
                    break
            remind_at = self.time_service.build_datetime("today", "evening")
            return {
                "transcript": text,
                "intents": [
                    {
                        "type": "create_reminder",
                        "text": reminder_text[:1].upper() + reminder_text[1:] if reminder_text else text,
                        "priority": "medium",
                        "remind_at": remind_at.isoformat(),
                    }
                ],
            }

        return {
            "transcript": text,
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
            ],
        }

    async def parse(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        parsed = await self.ai_service.parse_intents(text, context)
        normalized = []
        for intent in parsed.get("intents", []):
            intent = dict(intent)
            if intent.get("type") == "create_reminder":
                remind_at = self.time_service.build_datetime(intent.get("date"), intent.get("time"))
                intent["remind_at"] = remind_at.isoformat()
            if intent.get("type") in {"schedule_override", "rest_day"}:
                intent["override_date"] = self.time_service.parse_relative_date(intent.get("date")).isoformat()
            normalized.append(intent)
        return {"transcript": text, "intents": normalized}
