from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bot.database.queries import get_active_tasks, get_reminders_for_date, get_upcoming_overrides
from bot.services.time_service import TimeService

_ALLOWED_INTENT_TYPES = {
    "create_task", "create_reminder", "rest_day", "schedule_override",
    "show_today", "show_tasks", "do_nothing",
    "create_problem_block", "update_problem_block", "complete_problem_block",
    "archive_problem_block", "show_problem_blocks",
    "get_problem_solution", "get_problem_resources",
    "set_exam_date", "create_study_schedule_item",
}


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

            # Unknown intent type → re-run fallback for whole text
            if t not in _ALLOWED_INTENT_TYPES:
                return {"transcript": text, "intents": self.ai_service.parse_intent_fallback(text).get("intents", [])}

            # ── Normalize reminder ────────────────────────────────────────────
            if t == "create_reminder":
                remind_at = self.time_service.parse_datetime_any(item.get("remind_at"))
                if remind_at is None:
                    date_label = item.get("date") or ("tomorrow" if "завтра" in text.lower() else "today")
                    time_label = item.get("time") or ("morning" if "утр" in text.lower() else "evening")
                    if "через два дня" in text.lower() or "через 2 дня" in text.lower():
                        date_label = "через 2 дня"
                    remind_at = self.time_service.build_datetime(date_label, time_label)
                item["remind_at"] = remind_at.isoformat()
                item["priority"] = item.get("priority") or "medium"

            # ── Normalize rest_day ────────────────────────────────────────────
            if t == "rest_day":
                day = self.time_service.parse_relative_date(
                    item.get("date") or ("tomorrow" if "завтра" in text.lower() else "today")
                )
                item["override_date"] = day.isoformat()
                item["mode"] = "rest_day"
                item["create_tasks"] = False
                item["write_to_miro"] = False

            # ── Normalize problem block ───────────────────────────────────────
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

            # ── Normalize exam date ───────────────────────────────────────────
            if t == "set_exam_date":
                item["subject"] = (item.get("subject") or "Предмет").strip()
                if not item.get("exam_date"):
                    # Try to parse from free text
                    parsed_d = self.ai_service._parse_date_from_text(text.lower())
                    item["exam_date"] = parsed_d or ""

            # ── Normalize study schedule ──────────────────────────────────────
            if t == "create_study_schedule_item":
                item["subject"] = (item.get("subject") or "Предмет").strip()
                item["weekday"] = item.get("weekday") or "mon"
                item["time_str"] = item.get("time_str") or ""
                item["recurrence"] = item.get("recurrence") or "weekly"
                item["tutor_name"] = item.get("tutor_name") or ""

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
