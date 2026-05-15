from __future__ import annotations

from typing import Any

from bot.services.time_service import TimeService


class IntentParser:
    def __init__(self, ai_service, time_service: TimeService):
        self.ai_service = ai_service
        self.time_service = time_service

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
