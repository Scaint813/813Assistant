from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bot.database.queries import get_active_tasks, get_reminders_for_date, get_upcoming_overrides
from bot.services.time_service import TimeService


class IntentParser:
    def __init__(self, ai_service, time_service: TimeService):
        self.ai_service = ai_service
        self.time_service = time_service

    async def parse_user_text(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        base = await self._build_context(context)
        parsed = await self.ai_service.parse_intent_with_openai(text, base)
        if not parsed:
            parsed = self.ai_service.parse_intent_fallback(text)

        intents = parsed.get("intents", [])
        if not intents:
            intents = self.ai_service.parse_intent_fallback(text).get("intents", [])

        normalized = []
        for intent in intents:
            item = dict(intent)
            t = item.get("type")
            if t not in {"create_task", "create_reminder", "rest_day", "show_today", "show_tasks", "do_nothing", "create_problem_block", "update_problem_block", "complete_problem_block", "archive_problem_block", "show_problem_blocks", "get_problem_solution", "get_problem_resources"}:
                return {"transcript": text, "intents": self.ai_service.parse_intent_fallback(text).get("intents", [])}

            if t == "create_reminder":
                remind_at = self.time_service.parse_datetime_any(item.get("remind_at"))
                if remind_at is None:
                    date_label = item.get("date") or ("tomorrow" if "завтра" in text.lower() else "today")
                    time_label = item.get("time") or ("morning" if "утр" in text.lower() else "evening" if "веч" in text.lower() else "evening")
                    if "через два дня" in text.lower() or "через 2 дня" in text.lower():
                        date_label = "через 2 дня"
                    remind_at = self.time_service.build_datetime(date_label, time_label)
                item["remind_at"] = remind_at.isoformat()
                item["priority"] = item.get("priority") or "medium"

            if t == "rest_day":
                day = self.time_service.parse_relative_date(item.get("date") or ("tomorrow" if "завтра" in text.lower() else "today"))
                item["override_date"] = day.isoformat()
                item["mode"] = "rest_day"
                item["create_tasks"] = False
                item["write_to_miro"] = False
            if t == "create_problem_block":
                item["category"] = item.get("category") or "other"
                item["priority"] = item.get("priority") or "medium"
                item["pressure_level"] = item.get("pressure_level") or "normal"
                item["title"] = item.get("title") or text[:90]
                item["problem_text"] = item.get("problem_text") or text
                item["solution_strategy"] = item.get("solution_strategy") or "Короткий протокол решения."
                item["next_action"] = item.get("next_action") or "Сделать один короткий шаг."
                if item.get("deadline"):
                    parsed_deadline = self.time_service.parse_datetime_any(item.get("deadline"))
                    if parsed_deadline:
                        item["deadline"] = parsed_deadline.isoformat()

            normalized.append(item)

        return {"transcript": text, "intents": normalized}

    async def _build_context(self, context: dict[str, Any]) -> dict[str, Any]:
        session_factory = context.get("session_factory")
        user_id = context.get("user_id")
        now = self.time_service.now()
        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
        day_end = day_start + timedelta(days=1)

        active_tasks = []
        reminders = []
        overrides = []

        if session_factory and user_id:
            async with session_factory() as session:
                tasks = await get_active_tasks(session, user_id)
                rems = await get_reminders_for_date(session, user_id, day_start, day_end)
                ovs = await get_upcoming_overrides(session, user_id, now.date())
                active_tasks = [t.title for t in tasks[:10]]
                reminders = [r.text for r in rems[:10]]
                overrides = [f"{o.date}:{o.mode}" for o in ovs[:5]]

        return {
            "current_datetime": now.isoformat(),
            "timezone": str(self.time_service.tz),
            "today": now.date().isoformat(),
            "active_tasks": active_tasks,
            "today_reminders": reminders,
            "today_schedule_overrides": overrides,
        }
