from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select

from bot.database.models import MemoryEntry, Reminder, UserProfile, UserRuntimeState
from bot.database.queries import (
    get_active_tasks,
    get_upcoming_overrides,
)
from bot.services.ai_service import AI_ROUTING_POLICY_VERSION, AIService
from bot.services.content_quality import is_meaningful_label, task_title_from_request
from bot.services.entity_resolver import EntityResolver
from bot.services.intent_quality import (
    SAFE_INTENT_CLARIFICATION,
    confirmation_risk,
    is_assistant_feedback,
    looks_like_task_request,
)
from bot.services.preferences_service import PreferencesService
from bot.services.time_service import TimeService

_ALLOWED_INTENT_TYPES = {
    "create_task", "create_reminder", "rest_day", "schedule_override",
    "show_today", "show_tomorrow", "show_week", "show_tasks", "do_nothing",
    "show_reminders", "show_projects", "show_inbox", "show_help",
    "show_weekly_review", "show_next", "pick_task", "plan_day",
    "show_archive", "show_study", "show_automations", "show_settings",
    "update_task", "complete_task", "archive_task",
    "update_reminder", "cancel_reminder",
    "create_problem_block", "update_problem_block", "complete_problem_block",
    "archive_problem_block", "show_problem_blocks",
    "get_problem_solution", "get_problem_resources",
    "set_exam_date", "create_study_schedule_item",
    "decompose_project",
    "update_exam_date", "delete_exam_date", "delete_study_schedule_item",
}

PARSER_PROMPT_VERSION = "quality-v2-2026-08-01"


class IntentParser:
    def __init__(self, ai_service, time_service: TimeService):
        self.ai_service = ai_service
        self.time_service = time_service
        self.entity_resolver = EntityResolver()

    async def parse_user_text(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        if not context.get("_timezone_scoped"):
            timezone = await self._profile_timezone(context)
            scoped_context = {**context, "_timezone_scoped": True}
            scoped_parser = IntentParser(
                self.ai_service, self.time_service.in_timezone(timezone)
            )
            result = await scoped_parser.parse_user_text(text, scoped_context)
            result["user_timezone"] = timezone
            return result

        base = await self._build_context(context)
        deterministic_type = self._deterministic_query_type(text)
        if deterministic_type:
            parsed = {"intents": [{"type": deterministic_type}]}
            parser_path = "deterministic"
            usage = {}
        else:
            parsed = await self.ai_service.parse_intent_with_openai(text, base)
            parser_path = "openai"
            usage = dict(getattr(self.ai_service, "last_usage", {}) or {})
        if not parsed:
            parsed = self._fallback(text)
            parser_path = "fallback"

        intents = parsed.get("intents", [])
        if not intents and parsed.get("clarification"):
            return self._clarification_result(
                text, str(parsed["clarification"]), usage, parser_path
            )
        if not intents and parser_path == "openai":
            parsed = self._fallback(text)
            parser_path = "fallback"
            intents = parsed.get("intents", [])
            if not intents:
                return self._clarification_result(
                    text,
                    str(parsed.get("clarification") or SAFE_INTENT_CLARIFICATION),
                    usage,
                    parser_path,
                )

        source_parts = [
            re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", part).strip()
            for part in re.split(r"[\r\n]+", text)
            if part.strip()
        ]
        normalized = []
        for index, intent in enumerate(intents):
            item = dict(intent)
            t = item.get("type")
            inferred_source = (
                source_parts[index]
                if len(source_parts) == len(intents) and index < len(source_parts)
                else text
            )
            source_text = str(item.get("source_text") or inferred_source).strip()
            item["source_text"] = source_text

            # Models sometimes return only the semantic fragment in source_text.
            # For a single intent that can drop a short clarification appended to
            # the original request (for example, "сегодня в 18:20").  Temporal
            # evidence must therefore come from the complete user turn.  With
            # multiple intents, keep using the per-intent fragment so one item's
            # date or time cannot leak into another.
            temporal_text = text if len(intents) == 1 else source_text

            # Unknown intent type → re-run fallback for whole text
            if t not in _ALLOWED_INTENT_TYPES:
                return self._clarification_result(
                    text, SAFE_INTENT_CLARIFICATION, usage, parser_path
                )

            # ── Normalize reminder ────────────────────────────────────────────
            if t == "create_reminder":
                raw_remind_at = self.time_service.parse_datetime_any(item.get("remind_at"))
                explicit_date = self.time_service.extract_date_from_text(temporal_text)
                explicit_time = self.time_service.extract_time_from_text(temporal_text)
                lowered_source = temporal_text.casefold()
                relative_time = (
                    self.time_service.parse_snooze_text(temporal_text)
                    if re.search(r"\bчерез\s+\d+\s*(?:мин|час|ч\b|дн)", lowered_source)
                    else None
                )
                has_daypart = any(
                    word in lowered_source
                    for word in ("утром", "днём", "днем", "вечером", "ночью")
                )
                date_label = item.get("date")
                time_label = item.get("time")
                if explicit_time and explicit_date is None and str(date_label or "").lower() in {"today", "сегодня"}:
                    date_label = None
                if raw_remind_at is None and date_label is None and explicit_date is None:
                    date_label = None if explicit_time else "today"
                reminder_text = self._normalize_reminder_text(item.get("text"), source_text)
                if not reminder_text:
                    return self._clarification_result(
                        text,
                        "О чём напомнить? Напиши, например: «поход к врачу».",
                        usage,
                        parser_path,
                        repair={
                            "kind": "create_reminder",
                            "missing_field": "subject",
                            "original_text": source_text,
                        },
                    )
                if not explicit_time and not relative_time and not has_daypart:
                    return self._clarification_result(
                        text,
                        f"Когда напомнить про «{reminder_text}»? Например: «сегодня в 18:20».",
                        usage,
                        parser_path,
                        repair={
                            "kind": "create_reminder",
                            "missing_field": "time",
                            "original_text": temporal_text,
                            "context": {"subject": reminder_text},
                        },
                    )
                if has_daypart and not time_label:
                    time_label = "morning" if "утр" in lowered_source else (
                        "day" if "дн" in lowered_source else (
                            "night" if "ноч" in lowered_source else "evening"
                        )
                    )
                remind_at = relative_time or self.time_service.build_reminder_datetime(
                    temporal_text,
                    date_label=date_label,
                    time_label=time_label,
                    parsed=raw_remind_at,
                )
                item["text"] = reminder_text
                item["recurrence"] = self.time_service.parse_recurrence(temporal_text, item.get("recurrence"))
                item["timezone"] = str(self.time_service.tz)
                if item["recurrence"] == "weekly" and remind_at <= self.time_service.now():
                    remind_at += timedelta(days=7)
                elif item["recurrence"] in {"daily", "weekdays", "monthly"} and remind_at <= self.time_service.now():
                    remind_at = self.time_service.next_recurrence(
                        remind_at, item["recurrence"], self.time_service.now()
                    ) or remind_at
                item["remind_at"] = remind_at.isoformat()
                item["priority"] = item.get("priority") or "medium"

            if t == "create_task":
                if not looks_like_task_request(source_text):
                    return self._clarification_result(
                        text, SAFE_INTENT_CLARIFICATION, usage, parser_path
                    )
                title = (item.get("title") or item.get("text") or "").strip()[:255]
                cleaned_title = task_title_from_request(source_text)
                if is_meaningful_label(cleaned_title) and (
                    title.casefold() == source_text.strip().casefold()
                    or self._has_planning_details(source_text)
                ):
                    title = cleaned_title
                if not is_meaningful_label(title):
                    title = task_title_from_request(source_text)
                if not is_meaningful_label(title):
                    return self._clarification_result(
                        text,
                        "Что именно нужно сделать? Например: «подготовить документы к пятнице».",
                        usage,
                        parser_path,
                    )
                item["title"] = title
                item["outcome"] = (item.get("outcome") or item["title"]).strip()
                item["next_action"] = (item.get("next_action") or item["title"]).strip()
                if not is_meaningful_label(item["outcome"]):
                    item["outcome"] = item["title"]
                if not is_meaningful_label(item["next_action"]):
                    item["next_action"] = item["title"]
                item["importance"] = self._bounded_score(item.get("importance"), item.get("priority"))
                item["urgency"] = self._bounded_score(item.get("urgency"), item.get("priority"))
                explicit_minutes = self._explicit_duration(source_text)
                item["duration_confirmed"] = explicit_minutes is not None
                item["estimated_minutes"] = explicit_minutes or self._bounded_minutes(item.get("estimated_minutes"))
                deadline = self.time_service.parse_datetime_any(item.get("deadline"))
                explicit_date = self.time_service.extract_date_from_text(source_text)
                if deadline is None and explicit_date is not None:
                    explicit_clock = self.time_service.extract_time_from_text(source_text) or time(20)
                    deadline = datetime.combine(explicit_date, explicit_clock, tzinfo=self.time_service.tz)
                item["deadline"] = deadline.isoformat() if deadline else None
                item["deadline_confirmed"] = bool(deadline and self._has_explicit_deadline(source_text))
                item["planning_state"] = (
                    "ready" if item["duration_confirmed"] and item["deadline_confirmed"] else "inbox"
                )
                item["dependencies"] = [str(value)[:255] for value in (item.get("dependencies") or [])[:10]]
                item["blocked_reason"] = (item.get("blocked_reason") or "").strip()

            if t == "update_task":
                deadline = self.time_service.parse_datetime_any(item.get("deadline"))
                explicit_date = self.time_service.extract_date_from_text(source_text)
                if deadline is None and explicit_date is not None:
                    clock = self.time_service.extract_time_from_text(source_text) or time(20)
                    deadline = datetime.combine(explicit_date, clock, tzinfo=self.time_service.tz)
                if deadline:
                    item["deadline"] = deadline.isoformat()
                    item["deadline_confirmed"] = True
                minutes = self._explicit_duration(source_text)
                if minutes:
                    item["estimated_minutes"] = minutes
                    item["duration_confirmed"] = True
                if item.get("new_title"):
                    item["new_title"] = str(item["new_title"]).strip()[:255]
                if item.get("next_action"):
                    item["next_action"] = str(item["next_action"]).strip()[:1000]
                if item.get("project"):
                    item["project"] = str(item["project"]).strip()[:128]

            if t == "update_reminder":
                raw = self.time_service.parse_datetime_any(item.get("remind_at"))
                date_label = item.get("date")
                time_label = item.get("time")
                explicit_date = self.time_service.extract_date_from_text(source_text)
                explicit_time = self.time_service.extract_time_from_text(source_text)
                if raw is None and (date_label or time_label or explicit_date or explicit_time):
                    raw = self.time_service.build_reminder_datetime(
                        source_text, date_label=date_label, time_label=time_label, parsed=None
                    )
                if raw:
                    item["remind_at"] = raw.isoformat()
                    item["timezone"] = str(self.time_service.tz)
                if item.get("new_text"):
                    item["new_text"] = str(item["new_text"]).strip()[:1000]

            if t == "decompose_project":
                if not item.get("steps"):
                    plan = await self.ai_service.generate_task_plan(text, base)
                    plan_usage = dict(getattr(self.ai_service, "last_usage", {}) or {})
                    if plan_usage:
                        usage = {
                            "model": plan_usage.get("model") or usage.get("model", ""),
                            "input_tokens": int(usage.get("input_tokens") or 0) + int(plan_usage.get("input_tokens") or 0),
                            "output_tokens": int(usage.get("output_tokens") or 0) + int(plan_usage.get("output_tokens") or 0),
                        }
                    item.update(plan)
                normalized_steps = []
                for step in (item.get("steps") or [])[:10]:
                    step = dict(step)
                    title = (step.get("title") or "").strip()[:255]
                    if not title:
                        continue
                    step["title"] = title
                    step["outcome"] = (step.get("outcome") or title).strip()
                    step["next_action"] = (step.get("next_action") or title).strip()
                    step["importance"] = self._bounded_score(step.get("importance"), "medium")
                    step["urgency"] = self._bounded_score(step.get("urgency"), "medium")
                    step["estimated_minutes"] = self._bounded_minutes(step.get("estimated_minutes"))
                    step["duration_confirmed"] = True
                    step["deadline_confirmed"] = bool(step.get("deadline"))
                    step["planning_state"] = "ready"
                    step["dependencies"] = [str(value)[:255] for value in (step.get("dependencies") or [])[:10]]
                    step["blocked_reason"] = (step.get("blocked_reason") or "").strip()
                    normalized_steps.append(step)
                if not normalized_steps:
                    return self._clarification_result(
                        text,
                        "Не удалось получить конкретные шаги. Уточни желаемый результат проекта.",
                        usage,
                        parser_path,
                    )
                item["project"] = (item.get("project") or text)[:128]
                item["objective"] = (item.get("objective") or item["project"])[:1000]
                item["steps"] = normalized_steps

            # ── Normalize rest_day ────────────────────────────────────────────
            if t in {"rest_day", "schedule_override"}:
                raw_day = (
                    item.get("override_date")
                    or item.get("date")
                    or ("tomorrow" if "завтра" in text.lower() else "today")
                )
                try:
                    day = datetime.fromisoformat(str(raw_day)).date()
                except ValueError:
                    day = self.time_service.parse_relative_date(raw_day)
                item["override_date"] = day.isoformat()
                item["mode"] = item.get("mode") or (
                    "rest_day" if t == "rest_day" else "normal"
                )
                if t == "rest_day":
                    item["create_tasks"] = False
                    item["write_to_miro"] = False

            # ── Normalize problem block ───────────────────────────────────────
            if t == "create_problem_block":
                if is_assistant_feedback(source_text):
                    return self._clarification_result(
                        text,
                        "Похоже, это обратная связь об ассистенте. Ничего не сохраняю.",
                        usage,
                        parser_path,
                    )
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

        mutation_types = {
            "update_task", "complete_task", "archive_task",
            "update_reminder", "cancel_reminder",
        }
        if any(item.get("type") in mutation_types for item in normalized):
            session_factory = context.get("session_factory")
            user_id = context.get("user_id")
            if session_factory and user_id:
                resolved_items = []
                async with session_factory() as session:
                    for item in normalized:
                        resolved, clarification = await self.entity_resolver.resolve(
                            session, user_id, item
                        )
                        if clarification:
                            return self._clarification_result(
                                text, clarification, usage, parser_path
                            )
                        resolved_items.append(resolved)
                normalized = resolved_items

        return {
            "transcript": text,
            "intents": normalized,
            "usage": usage,
            "quality": {
                "parser_path": parser_path,
                "prompt_version": PARSER_PROMPT_VERSION,
                "routing_policy_version": AI_ROUTING_POLICY_VERSION,
                "clarification": False,
                "confirmation_risk": confirmation_risk(normalized),
            },
        }

    async def _profile_timezone(self, context: dict[str, Any]) -> str:
        session_factory = context.get("session_factory")
        user_id = context.get("user_id")
        if session_factory and user_id:
            async with session_factory() as session:
                result = await session.execute(
                    select(UserProfile.timezone).where(UserProfile.user_id == user_id)
                )
                timezone = result.scalar_one_or_none()
                if timezone:
                    try:
                        return str(self.time_service.in_timezone(timezone).tz)
                    except Exception:
                        pass
        return str(self.time_service.tz)

    def _fallback(self, text: str) -> dict[str, Any]:
        batch = getattr(self.ai_service, "parse_intent_fallback_batch", None)
        return batch(text) if batch else self.ai_service.parse_intent_fallback(text)

    @staticmethod
    def _clarification_result(
        text: str,
        clarification: str,
        usage: dict[str, Any],
        parser_path: str,
        repair: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "transcript": text,
            "intents": [],
            "clarification": clarification,
            "usage": usage,
            "quality": {
                "parser_path": parser_path,
                "prompt_version": PARSER_PROMPT_VERSION,
                "routing_policy_version": AI_ROUTING_POLICY_VERSION,
                "clarification": True,
            },
        }
        if repair:
            result["repair"] = repair
        return result

    @staticmethod
    def _explicit_duration(text: str) -> int | None:
        match = re.search(
            r"\b(?:на\s+)?(\d{1,3})\s*(?:мин(?:ут[уы]?)?|мин\.?|m)\b",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return max(5, min(10080, int(match.group(1))))
        hours = re.search(
            r"\b(?:на\s+)?(\d{1,3})(?:[.,](\d))?\s*(?:час(?:а|ов)?|ч\.?|h)\b",
            text,
            flags=re.IGNORECASE,
        )
        if hours:
            value = int(hours.group(1)) * 60 + (30 if hours.group(2) == "5" else 0)
            return max(5, min(10080, value))
        return None

    @staticmethod
    def _deterministic_query_type(text: str) -> str | None:
        """Protect read-only planning phrases from being turned into mutations by AI."""
        lowered = text.casefold().strip()
        if AIService._looks_like_day_plan_query(lowered):
            return "plan_day"
        if any(phrase in lowered for phrase in (
            "что у меня сегодня",
            "дела на сегодня",
            "план на сегодня",
            "расписание на сегодня",
            "покажи сегодня",
        )):
            return "show_today"
        if any(phrase in lowered for phrase in (
            "что у меня завтра",
            "дела на завтра",
            "план на завтра",
            "расписание на завтра",
            "покажи завтра",
        )):
            return "show_tomorrow"
        if any(phrase in lowered for phrase in (
            "что у меня на неделю",
            "дела на неделю",
            "план на неделю",
            "расписание на неделю",
            "покажи неделю",
            "ближайшие семь дней",
        )):
            return "show_week"
        if AIService._looks_like_task_pick_query(lowered):
            return "pick_task"
        return None

    def _has_explicit_deadline(self, text: str) -> bool:
        lowered = text.lower()
        return bool(
            self.time_service.extract_date_from_text(text)
            or re.search(r"\b(?:сегодня|завтра|послезавтра|до\s+\d|к\s+\d)\b", lowered)
            or re.search(
                r"\b(?:понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресенье)\b",
                lowered,
            )
        )

    def _has_planning_details(self, text: str) -> bool:
        return bool(self._explicit_duration(text) or self._has_explicit_deadline(text))

    @staticmethod
    def _normalize_reminder_text(candidate: str | None, original_text: str) -> str:
        """Keep the subject of a reminder and remove command/date/time boilerplate."""
        generic = {"", "напоминание", "напомнить", "reminder", "создать напоминание"}
        source = (candidate or "").strip()
        if source.lower().strip(" .!?:-") in generic or source == original_text:
            source = original_text

        cleaned = source
        cleaned = re.sub(
            r"^\s*(?:пожалуйста[, ]*)?(?:(?:поставь|создай|добавь)\s+)?(?:мне\s+)?напоминание\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"^\s*напомни(?:\s+мне)?\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"\b(?:сегодня|завтра|послезавтра|через\s+(?:два|2)\s+дня)\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\b(?:в|к|на)?\s*(?:[01]?\d|2[0-3])[.:][0-5]\d\b", "", cleaned)
        cleaned = re.sub(r"\b(?:в|к)\s*(?:[01]?\d|2[0-3])(?:\s*(?:час(?:а|ов)?|ч))?\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(?:напоминание|напомни(?:\s+мне)?)\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"\b(?:каждый\s+день|ежедневно|по\s+будням|каждый\s+месяц|ежемесячно)\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\bкажд(?:ый|ую|ое)\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\b(?:время|дата)\b\s*[:—–-]?", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*(?:на|о|об|про)\s+(?=\S)", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\n—–-:;,.")
        return "" if cleaned.lower().strip(" .!?:-") in generic else cleaned

    @staticmethod
    def _bounded_score(value, priority: str | None) -> int:
        defaults = {"urgent": 5, "high": 4, "medium": 3, "low": 2}
        try:
            return max(1, min(5, int(value)))
        except (TypeError, ValueError):
            return defaults.get(priority or "medium", 3)

    @staticmethod
    def _bounded_minutes(value) -> int:
        try:
            return max(5, min(10080, int(value)))
        except (TypeError, ValueError):
            return 30

    async def _build_context(self, context: dict[str, Any]) -> dict[str, Any]:
        session_factory = context.get("session_factory")
        user_id = context.get("user_id")
        now = self.time_service.now()
        active_tasks = []
        reminders = []
        overrides = []
        memory = []
        last_entity = None
        preferences = PreferencesService().get_all(None)

        if session_factory and user_id:
            async with session_factory() as session:
                profile_result = await session.execute(
                    select(UserProfile).where(UserProfile.user_id == user_id)
                )
                profile = profile_result.scalar_one_or_none()
                if profile is not None:
                    preferences = PreferencesService().get_all(profile)

                token_mode = preferences.get("token_mode", "balanced")
                limits = {
                    "economy": (5, 5, 3, 8),
                    "balanced": (10, 10, 5, 20),
                    "maximum": (20, 20, 10, 40),
                }
                task_limit, reminder_limit, override_limit, memory_limit = limits.get(
                    token_mode, limits["balanced"]
                )
                tasks = await get_active_tasks(session, user_id)
                ovs = await get_upcoming_overrides(session, user_id, now.date())
                memory_result = await session.execute(
                    select(MemoryEntry)
                    .where(MemoryEntry.user_id == user_id)
                    .order_by(MemoryEntry.updated_at.desc())
                    .limit(memory_limit)
                )
                active_tasks = [
                    {"id": task.id, "title": task.title, "deadline": task.deadline.isoformat() if task.deadline else None}
                    for task in tasks[:task_limit]
                ]
                active_reminder_result = await session.execute(
                    select(Reminder)
                    .where(Reminder.user_id == user_id, Reminder.status.in_(["active", "paused"]))
                    .order_by(Reminder.remind_at.asc())
                    .limit(reminder_limit)
                )
                reminders = [
                    {"id": reminder.id, "text": reminder.text, "remind_at": reminder.remind_at.isoformat()}
                    for reminder in active_reminder_result.scalars().all()
                ]
                overrides = [f"{o.date}:{o.mode}" for o in ovs[:override_limit]]
                memory = [
                    {"key": row.key, "value": row.value, "source": row.source}
                    for row in memory_result.scalars().all()
                ]
                runtime_result = await session.execute(
                    select(UserRuntimeState).where(UserRuntimeState.user_id == user_id)
                )
                runtime = runtime_result.scalar_one_or_none()
                last_entity = (
                    {"type": runtime.last_entity_type, "id": runtime.last_entity_id}
                    if runtime and runtime.last_entity_type and runtime.last_entity_id else None
                )
        return {
            "current_datetime": now.isoformat(),
            "timezone": str(self.time_service.tz),
            "today": now.date().isoformat(),
            "active_tasks": active_tasks,
            "today_reminders": reminders,
            "today_schedule_overrides": overrides,
            "explicit_memory": memory,
            "last_referenced_entity": last_entity,
            "assistant_preferences": preferences,
        }
