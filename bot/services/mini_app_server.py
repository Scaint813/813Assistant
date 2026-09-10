from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time as time_module
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qsl

from aiohttp import web
from sqlalchemy import func, select

from bot.database.models import (
    CalendarEvent,
    CalendarSyncState,
    DailyWellnessLog,
    HealthSnapshot,
    Reminder,
    Task,
    TaskPlanBlock,
    TrainingProfile,
    WorkoutSession,
)
from bot.services.content_quality import is_meaningful_task
from bot.services.datetime_utils import ensure_aware
from bot.services.preferences_service import PreferencesService

logger = logging.getLogger(__name__)


class MiniAppAuthError(ValueError):
    pass


class TelegramInitDataValidator:
    """Validate Telegram Mini App initData before trusting its user object."""

    def __init__(
        self,
        bot_token: str,
        allowed_user_ids,
        *,
        max_age_seconds: int = 24 * 60 * 60,
    ):
        self.bot_token = bot_token
        self.allowed_user_ids = frozenset(int(value) for value in allowed_user_ids)
        self.max_age_seconds = max(60, int(max_age_seconds))

    def validate(self, init_data: str, *, now_timestamp: int | None = None) -> int:
        if not init_data or len(init_data) > 16_384:
            raise MiniAppAuthError("missing Telegram authorization")
        try:
            pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise MiniAppAuthError("invalid Telegram authorization") from exc
        values = dict(pairs)
        if len(values) != len(pairs):
            raise MiniAppAuthError("duplicate Telegram authorization fields")
        received_hash = values.pop("hash", "")
        if not received_hash:
            raise MiniAppAuthError("missing Telegram authorization hash")
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(values.items())
        )
        secret_key = hmac.new(
            b"WebAppData", self.bot_token.encode(), hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(received_hash, expected_hash):
            raise MiniAppAuthError("invalid Telegram authorization signature")

        try:
            auth_date = int(values["auth_date"])
            user = json.loads(values["user"])
            user_id = int(user["id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MiniAppAuthError("incomplete Telegram authorization") from exc
        now_timestamp = int(now_timestamp or time_module.time())
        if auth_date > now_timestamp + 60:
            raise MiniAppAuthError("Telegram authorization is from the future")
        if now_timestamp - auth_date > self.max_age_seconds:
            raise MiniAppAuthError("Telegram authorization has expired")
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            raise MiniAppAuthError("user is outside the allowlist")
        return user_id


class MiniAppServer:
    PERIODS: ClassVar[dict[str, tuple[int, int]]] = {
        "today": (0, 1),
        "tomorrow": (1, 1),
        "week": (0, 7),
    }

    def __init__(
        self,
        host: str,
        port: int,
        bot_token: str,
        allowed_user_ids,
        session_factory,
        time_service,
        user_profile_service,
        calendar_service,
        conflict_service,
        hse_calendar_service,
        health_service=None,
        training_service=None,
        assistant_loop_service=None,
        *,
        calendar_bridge_token: str = "",
        calendar_bridge_owner_id: int | None = None,
        calendar_bridge_public_url: str = "",
        health_bridge_token: str = "",
        health_bridge_owner_id: int | None = None,
        health_bridge_public_url: str = "",
        dev_mode: bool = False,
    ):
        self.host = host
        self.port = int(port)
        self.allowed_user_ids = tuple(int(value) for value in allowed_user_ids)
        self.session_factory = session_factory
        self.time_service = time_service
        self.user_profile_service = user_profile_service
        self.calendar_service = calendar_service
        self.conflict_service = conflict_service
        self.hse_calendar_service = hse_calendar_service
        self.health_service = health_service
        self.training_service = training_service
        self.assistant_loop_service = assistant_loop_service
        default_owner_id = self.allowed_user_ids[0] if self.allowed_user_ids else 0
        self.calendar_bridge_owner_id = int(
            calendar_bridge_owner_id or default_owner_id
        )
        calendar_bridge_token = calendar_bridge_token.strip()
        self.calendar_bridge_token = (
            calendar_bridge_token
            if len(calendar_bridge_token) >= 32 and self.calendar_bridge_owner_id > 0
            else ""
        )
        self.calendar_bridge_public_url = calendar_bridge_public_url
        default_health_owner_id = self.allowed_user_ids[0] if self.allowed_user_ids else 0
        self.health_bridge_owner_id = int(
            health_bridge_owner_id or default_health_owner_id
        )
        health_bridge_token = health_bridge_token.strip()
        self.health_bridge_token = (
            health_bridge_token
            if len(health_bridge_token) >= 32 and self.health_bridge_owner_id > 0
            else ""
        )
        self.health_bridge_public_url = health_bridge_public_url
        self.preferences_service = PreferencesService()
        self.validator = TelegramInitDataValidator(bot_token, allowed_user_ids)
        self.dev_mode = dev_mode
        self.static_dir = Path(__file__).resolve().parents[1] / "miniapp"
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = self.create_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        logger.info("Mini App listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    def create_app(self) -> web.Application:
        app = web.Application(
            client_max_size=6 * 1024 * 1024,
            middlewares=[self._error_middleware, self._auth_middleware],
        )
        app.router.add_get("/", self._index)
        app.router.add_get("/healthz", self._health)
        app.router.add_get("/assets/{name}", self._asset)
        app.router.add_get("/api/v1/state", self._state)
        app.router.add_post("/api/v1/tasks", self._create_task)
        app.router.add_patch("/api/v1/tasks/{task_id}", self._update_task)
        app.router.add_get("/api/v1/profile", self._profile)
        app.router.add_patch("/api/v1/profile", self._update_profile)
        app.router.add_get("/api/v1/health", self._health_overview)
        app.router.add_post("/api/v1/health/intake", self._update_health_intake)
        app.router.add_patch("/api/v1/health/goals", self._update_health_goals)
        app.router.add_get(
            "/api/v1/health/shortcut-config",
            self._health_shortcut_config,
        )
        app.router.add_post("/api/v1/hse/import", self._import_hse)
        app.router.add_get(
            "/api/v1/hse/shortcut-config",
            self._hse_shortcut_config,
        )
        app.router.add_post(
            "/bridge/v1/hse-calendar",
            self._receive_hse_calendar,
        )
        app.router.add_post(
            "/bridge/v1/health-snapshot",
            self._receive_health_snapshot,
        )
        return app

    @web.middleware
    async def _error_middleware(self, request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response(
                {"error": exc.reason or "request failed"}, status=exc.status
            )
        except Exception:
            logger.exception("Mini App request failed: %s %s", request.method, request.path)
            response = web.json_response(
                {"error": "internal server error"}, status=500
            )
        if request.path.startswith(("/api/", "/bridge/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @web.middleware
    async def _auth_middleware(self, request, handler):
        if request.path.startswith("/bridge/v1/"):
            if request.path == "/bridge/v1/hse-calendar":
                bridge_token = self.calendar_bridge_token
                owner_id = self.calendar_bridge_owner_id
                disabled_reason = "calendar bridge is disabled"
            elif request.path == "/bridge/v1/health-snapshot":
                bridge_token = self.health_bridge_token
                owner_id = self.health_bridge_owner_id
                disabled_reason = "health bridge is disabled"
            else:
                raise web.HTTPNotFound()
            if not bridge_token:
                raise web.HTTPServiceUnavailable(reason=disabled_reason)
            expected = f"Bearer {bridge_token}"
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, expected):
                try:
                    payload = await request.json()
                except (json.JSONDecodeError, UnicodeDecodeError, web.HTTPBadRequest):
                    payload = None
                body_token = ""
                if isinstance(payload, dict):
                    body_token = str(payload.get("token") or "").strip()
                body_token_valid = hmac.compare_digest(
                    body_token,
                    bridge_token,
                ) or hmac.compare_digest(body_token, expected)
                if not body_token_valid:
                    raise web.HTTPUnauthorized(reason="invalid calendar bridge token")
                request["bridge_payload"] = payload
            request["user_id"] = owner_id
            return await handler(request)
        if not request.path.startswith("/api/v1/"):
            return await handler(request)
        if self.dev_mode and request.headers.get("X-Debug-User"):
            try:
                user_id = int(request.headers["X-Debug-User"])
            except ValueError as exc:
                raise web.HTTPUnauthorized(reason="invalid debug user") from exc
            if self.allowed_user_ids and user_id not in self.allowed_user_ids:
                raise web.HTTPForbidden(reason="user is outside the allowlist")
            request["user_id"] = user_id
            return await handler(request)
        authorization = request.headers.get("Authorization", "")
        prefix = "tma "
        if not authorization.startswith(prefix):
            raise web.HTTPUnauthorized(reason="open this Mini App from Telegram")
        try:
            request["user_id"] = self.validator.validate(
                authorization[len(prefix):]
            )
        except MiniAppAuthError as exc:
            raise web.HTTPUnauthorized(reason=str(exc)) from exc
        return await handler(request)

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        response = web.FileResponse(self.static_dir / "index.html")
        self._secure_static_headers(response)
        response.headers["Cache-Control"] = "no-cache"
        return response

    async def _asset(self, request: web.Request) -> web.StreamResponse:
        allowed = {"app.css", "app.js", "resource_store.js"}
        name = request.match_info["name"]
        if name not in allowed:
            raise web.HTTPNotFound()
        response = web.FileResponse(self.static_dir / name)
        self._secure_static_headers(response)
        response.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
        return response

    @staticmethod
    def _secure_static_headers(response: web.StreamResponse) -> None:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; "
            "style-src 'self'; img-src 'self' data: https:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors https://web.telegram.org "
            "https://*.telegram.org"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _state(self, request: web.Request) -> web.Response:
        period = request.query.get("period", "today")
        if period not in self.PERIODS:
            raise web.HTTPBadRequest(reason="unsupported period")
        user_id = request["user_id"]
        async with self.session_factory() as session:
            now, profile = await self._user_now(session, user_id)
            payload = await self._build_state(
                session, user_id, now, profile, period
            )
        return web.json_response(payload)

    async def _build_state(self, session, user_id, now, profile, period) -> dict:
        offset, days = self.PERIODS[period]
        first_day = now.date() + timedelta(days=offset)
        start = datetime.combine(first_day, time.min, tzinfo=now.tzinfo)
        end = start + timedelta(days=days)
        today_start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)

        event_result = await session.execute(
            select(CalendarEvent).where(
                CalendarEvent.user_id == user_id,
                CalendarEvent.start_at < end,
                CalendarEvent.end_at > start,
            ).order_by(CalendarEvent.start_at.asc())
        )
        events = list(event_result.scalars().all())
        reminder_result = await session.execute(
            select(Reminder).where(
                Reminder.user_id == user_id,
                Reminder.status == "active",
                Reminder.remind_at >= start,
                Reminder.remind_at < end,
            ).order_by(Reminder.remind_at.asc())
        )
        reminders = list(reminder_result.scalars().all())
        task_result = await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                (
                    (Task.status == "active")
                    | (
                        (Task.status == "completed")
                        & (Task.completed_at >= today_start)
                        & (Task.completed_at < today_start + timedelta(days=1))
                    )
                ),
            )
        )
        tasks = [item for item in task_result.scalars().all() if is_meaningful_task(item)]
        active_tasks = [item for item in tasks if item.status == "active"]
        active_tasks.sort(key=lambda item: self._task_sort_key(item, now))

        block_result = await session.execute(
            select(TaskPlanBlock).where(
                TaskPlanBlock.user_id == user_id,
                TaskPlanBlock.status == "planned",
                TaskPlanBlock.start_at < end,
                TaskPlanBlock.end_at > start,
            ).order_by(TaskPlanBlock.start_at.asc())
        )
        blocks = list(block_result.scalars().all())
        tasks_by_id = {item.id: item for item in active_tasks}
        workout_result = await session.execute(
            select(WorkoutSession).where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.status.in_(("planned", "in_progress")),
                WorkoutSession.scheduled_for >= start,
                WorkoutSession.scheduled_for < end,
            ).order_by(WorkoutSession.scheduled_for.asc())
        )
        workouts = list(workout_result.scalars().all())
        conflicts = await self.conflict_service.detect_between(
            session, user_id, start, end
        )
        hse_status = await self.hse_calendar_service.status(
            session,
            now,
            user_id=user_id,
        )
        health_snapshot = await session.scalar(
            select(HealthSnapshot)
            .where(HealthSnapshot.user_id == user_id)
            .order_by(
                HealthSnapshot.date.desc(),
                HealthSnapshot.captured_at.desc(),
            )
            .limit(1)
        )
        if not hse_status["has_complete_snapshot"]:
            events = [
                item for item in events
                if item.source not in {"hse_ical", "hse_ios"}
            ]

        timeline = []
        for event in events:
            timeline.append(self._event_payload(event, now.tzinfo))
        for reminder in reminders:
            at = ensure_aware(reminder.remind_at, now.tzinfo)
            timeline.append({
                "key": f"reminder:{reminder.id}",
                "kind": "reminder",
                "title": reminder.text,
                "start": at.isoformat(),
                "end": None,
                "meta": "Напоминание",
                "location": "",
            })
        for block in blocks:
            task = tasks_by_id.get(block.task_id)
            if task:
                timeline.append({
                    "key": f"block:{block.id}",
                    "kind": "task",
                    "title": task.title,
                    "start": ensure_aware(block.start_at, now.tzinfo).isoformat(),
                    "end": ensure_aware(block.end_at, now.tzinfo).isoformat(),
                    "meta": "Фокус",
                    "location": "",
                    "task_id": task.id,
                })
        for workout in workouts:
            at = ensure_aware(workout.scheduled_for, now.tzinfo)
            timeline.append({
                "key": f"workout:{workout.id}",
                "kind": "workout",
                "title": workout.title,
                "start": at.isoformat(),
                "end": None,
                "meta": "Тренировка",
                "location": "",
            })
        timeline.sort(key=lambda item: item["start"])

        next_hse_event = None
        if not timeline and hse_status["has_complete_snapshot"]:
            next_hse_event = await session.scalar(
                select(CalendarEvent).where(
                    CalendarEvent.user_id == user_id,
                    CalendarEvent.source.in_(("hse_ical", "hse_ios")),
                    CalendarEvent.end_at > now,
                ).order_by(CalendarEvent.start_at.asc()).limit(1)
            )

        serialized_tasks = [self._task_payload(item, now) for item in tasks]
        overdue_count = sum(
            1 for item in active_tasks
            if item.deadline and ensure_aware(item.deadline, now.tzinfo) < now
        )
        hse_count = sum(
            1 for item in events if item.source in {"hse_ical", "hse_ios"}
        )
        current_item = next(
            (
                item for item in timeline
                if item.get("end")
                and self.calendar_service._parse_datetime(item["start"]) <= now
                < self.calendar_service._parse_datetime(item["end"])
            ),
            None,
        )
        future_items = [
            item for item in timeline
            if self.calendar_service._parse_datetime(item["start"]) >= now
        ]
        next_item = future_items[0] if future_items else None
        if current_item is None and active_tasks:
            current_item = {
                "key": f"task:{active_tasks[0].id}",
                "kind": "task",
                "title": active_tasks[0].title,
                "start": None,
                "end": None,
                "meta": "Главный приоритет",
                "location": "",
                "task_id": active_tasks[0].id,
            }
        if next_item is None and next_hse_event is not None:
            next_item = self._event_payload(next_hse_event, now.tzinfo)

        planning = self.preferences_service.planning(profile)
        snapshot_payload = self._health_snapshot_payload(health_snapshot, now)
        recovery = self._recovery_payload(snapshot_payload, planning, now)
        risks = []
        if hse_status["integrity_status"] == "incomplete":
            risks.append({
                "level": "warning",
                "title": "Расписание HSE неполное",
                "text": hse_status["integrity_message"],
            })
        if conflicts:
            risks.append({
                "level": "danger",
                "title": f"Конфликтов в расписании: {len(conflicts)}",
                "text": "События пересекаются или между ними не хватает времени на дорогу.",
            })
        if overdue_count:
            risks.append({
                "level": "warning",
                "title": f"Просроченных задач: {overdue_count}",
                "text": "Их нужно перенести, завершить или вернуть в план.",
            })
        if recovery["level"] in {"watch", "overload"}:
            risks.append({
                "level": "warning" if recovery["level"] == "watch" else "danger",
                "title": recovery["title"],
                "text": recovery["message"],
            })
        return {
            "period": period,
            "now": now.isoformat(),
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "user": {
                "name": profile.name or "Пользователь",
                "timezone": profile.timezone,
            },
            "stats": {
                "events": len(events),
                "hse_events": hse_count,
                "active_tasks": len(active_tasks),
                "overdue_tasks": overdue_count,
                "conflicts": len(conflicts),
            },
            "timeline": timeline,
            "tasks": serialized_tasks,
            "focus": [self._task_payload(item, now) for item in active_tasks[:5]],
            "conflicts": [self._conflict_payload(item, now.tzinfo) for item in conflicts],
            "upcoming": {
                "next_hse_event": (
                    self._event_payload(next_hse_event, now.tzinfo)
                    if next_hse_event else None
                ),
            },
            "sources": {
                "hse": {
                    "status": hse_status["integrity_status"],
                    "message": hse_status["integrity_message"],
                    "events": hse_status["events"],
                    "received_count": hse_status["received_count"],
                    "synced_at": (
                        hse_status["synced_at"].isoformat()
                        if hse_status["synced_at"] else None
                    ),
                    "checked_at": (
                        hse_status["last_attempt_at"].isoformat()
                        if hse_status["last_attempt_at"] else None
                    ),
                },
            },
            "assistant": {
                "now": current_item,
                "next": next_item,
                "risks": risks,
                "resource": {
                    "connected": snapshot_payload is not None,
                    "level": recovery["level"],
                    "title": recovery["title"],
                    "text": recovery["message"],
                    "sleep_minutes": (
                        snapshot_payload.get("sleep_minutes", 0)
                        if snapshot_payload else 0
                    ),
                    "steps": (
                        snapshot_payload.get("steps", 0)
                        if snapshot_payload else 0
                    ),
                },
            },
        }

    async def _create_task(self, request: web.Request) -> web.Response:
        payload = await self._json_object(request)
        title = " ".join(str(payload.get("title") or "").split())
        if not 2 <= len(title) <= 255:
            raise web.HTTPBadRequest(reason="task title must contain 2–255 characters")
        user_id = request["user_id"]
        async with self.session_factory() as session:
            now, _profile = await self._user_now(session, user_id)
            deadline = self._parse_deadline(payload.get("deadline"), now.tzinfo)
            duration_value = payload.get("estimated_minutes")
            duration = self._bounded_int(duration_value, 5, 10_080) if duration_value else 30
            duration_confirmed = duration_value not in (None, "")
            task = Task(
                user_id=user_id,
                title=title,
                deadline=deadline,
                deadline_confirmed=deadline is not None,
                estimated_minutes=duration,
                duration_confirmed=duration_confirmed,
                planning_state="ready" if deadline or duration_confirmed else "inbox",
            )
            session.add(task)
            await session.flush()
            await self._rebuild_plan(session, user_id, now, reason="task_created")
            await session.commit()
            response = self._task_payload(task, now)
        return web.json_response(response, status=201)

    async def _update_task(self, request: web.Request) -> web.Response:
        try:
            task_id = int(request.match_info["task_id"])
        except ValueError as exc:
            raise web.HTTPBadRequest(reason="invalid task id") from exc
        payload = await self._json_object(request)
        user_id = request["user_id"]
        async with self.session_factory() as session:
            now, _profile = await self._user_now(session, user_id)
            result = await session.execute(
                select(Task).where(Task.id == task_id, Task.user_id == user_id)
            )
            task = result.scalar_one_or_none()
            if task is None:
                raise web.HTTPNotFound(reason="task not found")
            if "status" in payload:
                status = str(payload["status"])
                if status not in {"active", "completed"}:
                    raise web.HTTPBadRequest(reason="unsupported task status")
                task.status = status
                task.completed_at = now if status == "completed" else None
                if status == "completed":
                    task.scheduled_start = None
                    task.scheduled_end = None
            if "title" in payload:
                title = " ".join(str(payload["title"] or "").split())
                if not 2 <= len(title) <= 255:
                    raise web.HTTPBadRequest(reason="invalid task title")
                task.title = title
            if "deadline" in payload:
                task.deadline = self._parse_deadline(payload["deadline"], now.tzinfo)
                task.deadline_confirmed = task.deadline is not None
            if "estimated_minutes" in payload:
                task.estimated_minutes = self._bounded_int(
                    payload["estimated_minutes"], 5, 10_080
                )
                task.duration_confirmed = True
            await self._rebuild_plan(session, user_id, now, reason="task_updated")
            await session.commit()
            response = self._task_payload(task, now)
        return web.json_response(response)

    async def _profile(self, request: web.Request) -> web.Response:
        user_id = request["user_id"]
        async with self.session_factory() as session:
            now, profile = await self._user_now(session, user_id)
            hse_status = await self.hse_calendar_service.status(
                session, now, user_id=user_id
            )
            hse_synced_at = hse_status["synced_at"]
            hse_sync_age_minutes = (
                max(0, int((now - hse_synced_at).total_seconds() // 60))
                if hse_synced_at
                else None
            )
            if hse_status["integrity_status"] == "incomplete":
                hse_sync_status = "incomplete"
            elif hse_sync_age_minutes is None:
                hse_sync_status = "never"
            elif hse_sync_age_minutes <= 36 * 60:
                hse_sync_status = "fresh"
            else:
                hse_sync_status = "stale"
            planning = self.preferences_service.planning(profile)
            try:
                context = json.loads(profile.preferences_json or "{}")
            except json.JSONDecodeError:
                context = {}
        return web.json_response({
            "name": profile.name or "Пользователь",
            "timezone": profile.timezone,
            "timezone_options": self.user_profile_service.options(now),
            "home": str(context.get("home") or ""),
            "planning": {
                "daily_focus_minutes": planning["daily_focus_minutes"],
                "auto_planning_minutes": planning["auto_planning_minutes"],
                "focus_load_level": planning["focus_load_level"],
                "focus_warning": planning["focus_warning"],
                "workday_start_hour": planning["workday_start_hour"],
                "workday_end_hour": planning["workday_end_hour"],
            },
            "hse": {
                "events": hse_status["events"],
                "synced_at": (
                    hse_synced_at.isoformat()
                    if hse_synced_at else None
                ),
                "sync_status": hse_sync_status,
                "sync_age_minutes": hse_sync_age_minutes,
                "last_result": hse_status["last_result"],
                "received_count": hse_status["received_count"],
                "window_days": hse_status["window_days"],
                "integrity_status": hse_status["integrity_status"],
                "integrity_reason": hse_status["integrity_reason"],
                "integrity_message": hse_status["integrity_message"],
                "last_attempt_at": (
                    hse_status["last_attempt_at"].isoformat()
                    if hse_status["last_attempt_at"] else None
                ),
                "last_complete_at": (
                    hse_status["last_complete_at"].isoformat()
                    if hse_status["last_complete_at"] else None
                ),
                "consecutive_failures": hse_status["consecutive_failures"],
                "periodic_sync": hse_status["configured"],
                "iphone_bridge": bool(
                    self.calendar_bridge_token
                    and user_id == self.calendar_bridge_owner_id
                ),
                "calendar_name": "HSE",
                "first_start": (
                    hse_status["first_start"].isoformat()
                    if hse_status["first_start"] else None
                ),
                "last_end": (
                    hse_status["last_end"].isoformat()
                    if hse_status["last_end"] else None
                ),
            },
        })

    async def _update_profile(self, request: web.Request) -> web.Response:
        payload = await self._json_object(request)
        user_id = request["user_id"]
        async with self.session_factory() as session:
            profile = await self.user_profile_service.get(session, user_id)
            if "timezone" in payload:
                requested = str(payload["timezone"])
                valid_options = {
                    item["timezone"] for item in self.user_profile_service.options()
                }
                if requested not in valid_options:
                    raise web.HTTPBadRequest(reason="unsupported timezone")
                profile.timezone = requested
            if "daily_focus_minutes" in payload:
                self.preferences_service.set(
                    profile,
                    "daily_focus_minutes",
                    self._bounded_int(payload["daily_focus_minutes"], 60, 1440),
                )
            if "home" in payload:
                try:
                    context = json.loads(profile.preferences_json or "{}")
                except json.JSONDecodeError:
                    context = {}
                start_point = " ".join(str(payload["home"] or "").split())[:255]
                context["home"] = (
                    start_point[:1].upper() + start_point[1:]
                    if start_point
                    else ""
                )
                profile.preferences_json = json.dumps(context, ensure_ascii=False)
            now = self.time_service.in_timezone(
                self.user_profile_service.valid_timezone(profile.timezone)
            ).now()
            await self._rebuild_plan(session, user_id, now, reason="profile_updated")
            await session.commit()
        return await self._profile(request)

    async def _health_overview(self, request: web.Request) -> web.Response:
        async with self.session_factory() as session:
            payload = await self._health_payload(session, request["user_id"])
        return web.json_response(payload)

    async def _health_payload(self, session, user_id: int) -> dict:
        now, profile = await self._user_now(session, user_id)
        snapshot = await session.scalar(
            select(HealthSnapshot)
            .where(HealthSnapshot.user_id == user_id)
            .order_by(
                HealthSnapshot.date.desc(),
                HealthSnapshot.captured_at.desc(),
            )
            .limit(1)
        )
        daily_log = await session.scalar(
            select(DailyWellnessLog).where(
                DailyWellnessLog.user_id == user_id,
                DailyWellnessLog.date == now.date(),
            )
        )
        training_profile = await session.scalar(
            select(TrainingProfile).where(TrainingProfile.user_id == user_id)
        )
        upcoming_result = await session.execute(
            select(WorkoutSession)
            .where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.status.in_(("planned", "in_progress")),
                WorkoutSession.scheduled_for >= now - timedelta(days=1),
                WorkoutSession.scheduled_for < now + timedelta(days=14),
            )
            .order_by(WorkoutSession.scheduled_for.asc())
            .limit(8)
        )
        upcoming = list(upcoming_result.scalars().all())
        history_result = await session.execute(
            select(WorkoutSession)
            .where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.status.in_(("completed", "skipped")),
            )
            .order_by(
                WorkoutSession.completed_at.desc(),
                WorkoutSession.scheduled_for.desc(),
            )
            .limit(6)
        )
        history = list(history_result.scalars().all())
        planning = self.preferences_service.planning(profile)
        goals = self.preferences_service.wellness_goals(profile)
        snapshot_payload = self._health_snapshot_payload(snapshot, now)
        workout_payloads = [
            self._workout_payload(item, now) for item in upcoming
        ]
        history_payloads = [
            self._workout_payload(item, now) for item in history
        ]
        recovery = self._recovery_payload(
            snapshot_payload,
            planning,
            now,
        )
        return {
            "now": now.isoformat(),
            "timezone": getattr(now.tzinfo, "key", str(now.tzinfo)),
            "apple_health": {
                "connected": snapshot is not None,
                "bridge_enabled": bool(
                    self.health_bridge_token
                    and user_id == self.health_bridge_owner_id
                ),
                "snapshot": snapshot_payload,
            },
            "recovery": recovery,
            "intake": {
                "water_ml": daily_log.water_ml if daily_log else 0,
                "protein_g": daily_log.protein_g if daily_log else 0,
                "calories_kcal": daily_log.calories_kcal if daily_log else 0,
                "fat_g": daily_log.fat_g if daily_log else 0,
                "carbs_g": daily_log.carbs_g if daily_log else 0,
            },
            "goals": goals,
            "training": {
                "configured": bool(training_profile and training_profile.active),
                "goal": training_profile.goal if training_profile else "",
                "upcoming": workout_payloads,
                "history": history_payloads,
                "micro_plan": self._training_micro_plan(
                    training_profile,
                    workout_payloads,
                    history_payloads,
                    snapshot_payload,
                ),
            },
            "medical_disclaimer": (
                "Показатели используются для бытового планирования, а не для "
                "диагностики или назначения лечения."
            ),
        }

    async def _update_health_intake(self, request: web.Request) -> web.Response:
        payload = await self._json_object(request)
        limits = {
            "water_ml": (-2_000, 2_000, 20_000),
            "protein_g": (-200, 200, 1_000),
            "calories_kcal": (-5_000, 5_000, 20_000),
            "fat_g": (-300, 300, 1_000),
            "carbs_g": (-500, 500, 2_000),
        }
        deltas = {}
        for field, (minimum, maximum, _total_maximum) in limits.items():
            if field in payload:
                deltas[field] = self._bounded_int(
                    payload[field], minimum, maximum
                )
        if not deltas:
            raise web.HTTPBadRequest(reason="at least one intake delta is required")
        user_id = request["user_id"]
        async with self.session_factory() as session:
            now, _profile = await self._user_now(session, user_id)
            daily_log = await session.scalar(
                select(DailyWellnessLog).where(
                    DailyWellnessLog.user_id == user_id,
                    DailyWellnessLog.date == now.date(),
                )
            )
            if daily_log is None:
                daily_log = DailyWellnessLog(user_id=user_id, date=now.date())
                session.add(daily_log)
            for field, delta in deltas.items():
                total_maximum = limits[field][2]
                current = int(getattr(daily_log, field) or 0)
                setattr(
                    daily_log,
                    field,
                    max(0, min(total_maximum, current + delta)),
                )
            await session.commit()
            result = await self._health_payload(session, user_id)
        return web.json_response(result)

    async def _update_health_goals(self, request: web.Request) -> web.Response:
        payload = await self._json_object(request)
        user_id = request["user_id"]
        async with self.session_factory() as session:
            profile = await self.user_profile_service.get(session, user_id)
            if "water_ml" in payload:
                self.preferences_service.set(
                    profile,
                    "water_goal_ml",
                    self._bounded_int(payload["water_ml"], 0, 10_000),
                )
            if "protein_g" in payload:
                self.preferences_service.set(
                    profile,
                    "protein_goal_g",
                    self._bounded_int(payload["protein_g"], 0, 400),
                )
            await session.commit()
            result = await self._health_payload(session, user_id)
        return web.json_response(result)

    async def _health_shortcut_config(self, request: web.Request) -> web.Response:
        if (
            not self.health_bridge_token
            or request["user_id"] != self.health_bridge_owner_id
        ):
            raise web.HTTPForbidden(reason="health bridge is unavailable")
        return web.json_response({
            "endpoint": self.health_bridge_public_url,
            "token": self.health_bridge_token,
            "morning_trigger": "08:00",
            "evening_trigger": "18:00",
            "fields": [
                "date",
                "steps",
                "step_goal",
                "sleep_minutes",
                "workout_minutes",
                "active_energy_kcal",
                "resting_heart_rate",
                "hrv_ms",
            ],
        })

    async def _receive_health_snapshot(self, request: web.Request) -> web.Response:
        if self.health_service is None:
            raise web.HTTPServiceUnavailable(reason="health service is disabled")
        payload = request.get("bridge_payload")
        if payload is None:
            payload = await self._json_object(request)
        async with self.session_factory() as session:
            try:
                row = await self.health_service.upsert_snapshot(
                    session,
                    request["user_id"],
                    payload,
                )
            except (TypeError, ValueError) as exc:
                raise web.HTTPBadRequest(reason=f"invalid health snapshot: {exc}") from exc
            await session.commit()
            result = {
                "status": "saved",
                "date": row.date.isoformat(),
                "steps": row.steps,
                "sleep_minutes": row.sleep_minutes,
            }
        return web.json_response(result)

    @staticmethod
    def _health_snapshot_payload(snapshot: HealthSnapshot | None, now: datetime) -> dict | None:
        if snapshot is None:
            return None
        captured_at = ensure_aware(snapshot.captured_at, now.tzinfo)
        return {
            "date": snapshot.date.isoformat(),
            "fresh": snapshot.date == now.date(),
            "captured_at": captured_at.isoformat() if captured_at else None,
            "steps": snapshot.steps,
            "step_goal": snapshot.step_goal,
            "active_energy_kcal": snapshot.active_energy_kcal,
            "workout_minutes": snapshot.workout_minutes,
            "sleep_minutes": snapshot.sleep_minutes,
            "resting_heart_rate": snapshot.resting_heart_rate,
            "hrv_ms": snapshot.hrv_ms,
        }

    @staticmethod
    def _recovery_payload(snapshot: dict | None, planning: dict, now: datetime) -> dict:
        recommendations = []
        level = "good"
        title = "Нагрузка выглядит управляемой"
        if planning.get("focus_warning"):
            recommendations.append(planning["focus_warning"])
            level = "overload" if planning["focus_load_level"] == "overload" else "watch"
            title = "В профиле задана высокая нагрузка"
        if not snapshot:
            return {
                "level": "unknown",
                "title": "Нет данных Apple Health",
                "message": "Подключи сон и шаги, чтобы советы опирались на фактическое восстановление.",
                "recommendations": recommendations,
            }
        if not snapshot["fresh"]:
            return {
                "level": "unknown",
                "title": "Данные Apple Health устарели",
                "message": "Запусти синхронизацию: старые показатели не используются для решений о нагрузке.",
                "recommendations": recommendations,
            }
        sleep_minutes = int(snapshot.get("sleep_minutes") or 0)
        if 0 < sleep_minutes < 360:
            level = "overload"
            title = "Сегодня восстановление важнее объёма"
            recommendations.insert(
                0,
                "Сон меньше 6 часов: сократи интенсивную нагрузку и освободи время для сна.",
            )
        elif 0 < sleep_minutes < 420 and level != "overload":
            level = "watch"
            title = "Сон ниже желаемого"
            recommendations.insert(
                0,
                "Не наращивай нагрузку автоматически; оставь запас на паузы и более ранний сон.",
            )
        step_goal = int(snapshot.get("step_goal") or 0)
        steps = int(snapshot.get("steps") or 0)
        if (
            now.hour >= 18
            and step_goal > 0
            and steps < step_goal
            and int(snapshot.get("workout_minutes") or 0) < 30
        ):
            gap = step_goal - steps
            recommendations.append(
                f"До личной цели не хватает {gap:,} шагов — подойдёт спокойная прогулка."
                .replace(",", " ")
            )
            if level == "good":
                level = "watch"
                title = "Стоит добавить немного движения"
        message = (
            "Сигналы основаны на сегодняшнем сне, движении и выбранной рабочей нагрузке."
        )
        if not recommendations:
            recommendations.append(
                "Сохраняй обычный режим и повышай нагрузку постепенно."
            )
        return {
            "level": level,
            "title": title,
            "message": message,
            "recommendations": recommendations,
        }

    @staticmethod
    def _workout_payload(workout: WorkoutSession, now: datetime) -> dict:
        try:
            details = json.loads(workout.details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        exercises = []
        for exercise in details.get("exercises", [])[:6]:
            if not isinstance(exercise, dict):
                continue
            exercises.append({
                "name": str(exercise.get("name") or "Упражнение")[:255],
                "sets": int(exercise.get("sets") or 0),
                "reps": str(exercise.get("reps") or ""),
                "progression": str(exercise.get("progression") or ""),
                "progression_note": str(exercise.get("progression_note") or ""),
                "suggested_weight_kg": str(
                    exercise.get("suggested_weight_kg") or ""
                ),
            })
        scheduled_for = ensure_aware(workout.scheduled_for, now.tzinfo)
        completed_at = ensure_aware(workout.completed_at, now.tzinfo)
        return {
            "id": workout.id,
            "title": workout.title,
            "status": workout.status,
            "scheduled_for": scheduled_for.isoformat(),
            "completed_at": completed_at.isoformat() if completed_at else None,
            "rpe": workout.rpe,
            "notes": workout.notes,
            "activity_type": workout.activity_type,
            "load_level": workout.load_level,
            "duration_minutes": int(details.get("estimated_minutes") or 0),
            "focus": str(details.get("focus") or ""),
            "exercises": exercises,
        }

    @staticmethod
    def _training_micro_plan(
        profile: TrainingProfile | None,
        upcoming: list[dict],
        history: list[dict],
        snapshot: dict | None,
    ) -> dict:
        if profile is None or not profile.active:
            return {
                "level": "setup",
                "title": "Сначала задай тренировочную цель",
                "text": "Напиши боту «составь план тренировок» — готовый план появится здесь.",
            }
        if not upcoming:
            return {
                "level": "empty",
                "title": "Нет ближайшей тренировки",
                "text": "Собери новую тренировочную неделю в чате с ботом.",
            }
        next_workout = upcoming[0]
        sleep_minutes = int((snapshot or {}).get("sleep_minutes") or 0)
        if (snapshot or {}).get("fresh") and 0 < sleep_minutes < 360:
            return {
                "level": "recovery",
                "title": "Облегчи ближайшую тренировку",
                "text": "После короткого сна не повышай вес: сократи объём или выбери спокойное восстановление.",
            }
        guidance = [
            item["progression_note"]
            for item in next_workout["exercises"]
            if item.get("progression_note")
        ]
        if guidance:
            return {
                "level": "progress",
                "title": "Микро-план прогрессии",
                "text": " ".join(guidance[:2]),
            }
        latest_completed = next(
            (item for item in history if item["status"] == "completed"),
            None,
        )
        if latest_completed and int(latest_completed.get("rpe") or 0) >= 9:
            text = "Прошлая тренировка была тяжёлой: сохрани или немного снизь нагрузку."
        else:
            text = "Сохрани чистую технику; повышай только один параметр — вес или повторы."
        return {"level": "steady", "title": "Следующий небольшой шаг", "text": text}

    async def _import_hse(self, request: web.Request) -> web.Response:
        content = await request.read()
        if not content:
            raise web.HTTPBadRequest(reason="empty iCal file")
        try:
            result = await self.hse_calendar_service.import_bytes(
                content, user_id=request["user_id"]
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from exc
        return web.json_response({
            "imported": result.imported,
            "first_start": result.first_start.isoformat() if result.first_start else None,
            "last_end": result.last_end.isoformat() if result.last_end else None,
        })

    async def _hse_shortcut_config(self, request: web.Request) -> web.Response:
        if request["user_id"] != self.calendar_bridge_owner_id:
            raise web.HTTPForbidden(reason="calendar bridge belongs to the owner")
        if not self.calendar_bridge_token or not self.calendar_bridge_public_url:
            raise web.HTTPServiceUnavailable(reason="calendar bridge is disabled")
        return web.json_response({
            "calendar": "HSE",
            "endpoint": self.calendar_bridge_public_url,
            "authorization": f"Bearer {self.calendar_bridge_token}",
            "token": self.calendar_bridge_token,
            "trigger": "HSE App is closed",
            "daily_trigger": "07:00",
            "run_immediately": True,
            "silent": True,
            "window_days": 14,
        })

    async def _receive_hse_calendar(self, request: web.Request) -> web.Response:
        def decode_shortcut_json(value):
            """Remove the extra JSON escaping added by iOS Shortcuts."""
            decoded = value
            for _ in range(4):
                if not isinstance(decoded, str):
                    return decoded
                text = decoded.strip()
                try:
                    candidate = json.loads(text, strict=False)
                except json.JSONDecodeError:
                    if r'\"' not in text:
                        candidate = self._decode_shortcut_object_text(text)
                    else:
                        try:
                            candidate = json.loads(f'"{text}"', strict=False)
                        except json.JSONDecodeError:
                            candidate = self._decode_shortcut_object_text(text)
                    if candidate is None:
                        return decoded
                if candidate == decoded:
                    return decoded
                decoded = candidate
            return decoded

        payload = request.get("bridge_payload")
        if payload is None:
            try:
                payload = await request.json()
            except (json.JSONDecodeError, web.HTTPBadRequest) as exc:
                raise web.HTTPBadRequest(reason="JSON expected") from exc
        raw_events = payload.get("events") if isinstance(payload, dict) else payload
        # A JSON body field configured as Text in Shortcuts can stringify the
        # repeat output. Accept valid JSON text in addition to native values.
        if isinstance(raw_events, str):
            decoded_events = decode_shortcut_json(raw_events)
            if isinstance(decoded_events, (dict, list)):
                raw_events = decoded_events
        # Shortcuts collapses a one-item Repeat Result into the item itself.
        # Treat that dictionary exactly like a one-element event list.
        if isinstance(raw_events, dict):
            raw_events = [raw_events]
        if not isinstance(raw_events, list):
            raise web.HTTPBadRequest(reason="events must be a list or object")

        # An Array field containing a list variable becomes a nested list in
        # Shortcuts. It can also stringify individual dictionary items. Flatten
        # both representations before validating the event fields.
        pending = list(reversed(raw_events))
        raw_events = []
        visited = 0
        while pending:
            visited += 1
            if visited > 4_000:
                raise web.HTTPBadRequest(reason="too many event values")
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(reversed(item))
                continue
            if isinstance(item, str):
                decoded_item = decode_shortcut_json(item)
                if isinstance(decoded_item, (dict, list)):
                    pending.append(decoded_item)
                    continue
            raw_events.append(item)
        if len(raw_events) > 2_000:
            raise web.HTTPBadRequest(reason="too many events")

        normalized_events = []
        ignored = 0
        rejection_reasons: dict[str, int] = {}
        text_samples: list[str] = []

        def reject(reason: str) -> None:
            nonlocal ignored
            ignored += 1
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        for raw in raw_events:
            if not isinstance(raw, dict):
                if isinstance(raw, str) and len(text_samples) < 2:
                    sample = " ".join(raw.split())[:240]
                    sample = re.sub(
                        r"(?i)\bBearer\s+\S+", "Bearer [redacted]", sample
                    )
                    sample = re.sub(
                        r"(?i)\b[a-f0-9]{24,}\b", "[identifier]", sample
                    )
                    text_samples.append(sample or "[empty]")
                reject(f"not_object:{type(raw).__name__}")
                continue
            values = self._normalized_shortcut_keys(raw)
            calendar_name = str(
                values.get("calendar") or values.get("calendar_name") or ""
            ).strip()
            if calendar_name.casefold() != "hse":
                reject("calendar")
                continue
            title = " ".join(str(values.get("title") or "").split())
            start = values.get("start") or values.get("start_date")
            end = values.get("end") or values.get("end_date")
            missing = [
                field
                for field, value in (("title", title), ("start", start), ("end", end))
                if not value
            ]
            if missing:
                reject("missing_" + "_".join(missing))
                continue
            try:
                start_at = self.calendar_service._parse_datetime(start)
                end_at = self.calendar_service._parse_datetime(end)
            except (TypeError, ValueError):
                reject("invalid_date")
                continue
            if end_at <= start_at:
                reject("invalid_range")
                continue

            notes = str(
                values.get("notes") or values.get("description") or ""
            ).strip()
            identifier = str(
                values.get("id") or values.get("identifier") or ""
            ).strip()
            if not identifier:
                note_id = re.search(r"\b[a-fA-F0-9]{24,128}\b", notes)
                identifier = note_id.group(0) if note_id else ""
            if not identifier:
                stable_value = f"{title}\n{start_at.isoformat()}\n{end_at.isoformat()}"
                identifier = hashlib.sha256(stable_value.encode()).hexdigest()

            location = " ".join(str(values.get("location") or "").split())
            building, room = self.hse_calendar_service._split_location(location)
            teacher = self.hse_calendar_service._extract_teacher(notes)
            if not teacher:
                teacher = self._teacher_from_hse_title(title)
            normalized_events.append({
                "id": f"hse-ios:{identifier}"[:255],
                "calendar": "HSE",
                "title": title[:255],
                "event_type": self.hse_calendar_service._event_type(title, notes),
                "description": notes[:4_000],
                "location": location[:255],
                "teacher": teacher[:255],
                "building": building[:255],
                "room": room[:64],
                "start": start_at.isoformat(),
                "end": end_at.isoformat(),
                "is_busy": True,
            })

        if raw_events and not normalized_events:
            diagnostic = ", ".join(
                f"{reason}={count}"
                for reason, count in sorted(rejection_reasons.items())
            ) or "empty"
            if text_samples:
                diagnostic += f"; text={text_samples[0]}"
            logger.warning(
                "HSE Shortcut payload rejected: user=%s reasons=%s keys=%s",
                request["user_id"],
                diagnostic,
                [
                    sorted(self._normalized_shortcut_keys(item))
                    for item in raw_events[:3]
                    if isinstance(item, dict)
                ],
            )
            raise web.HTTPBadRequest(
                reason=f"no valid HSE events: {diagnostic}"
            )
        normalized_events.sort(
            key=lambda item: (item["id"], item["start"], item["end"])
        )
        window_days = self.hse_calendar_service.DEFAULT_WINDOW_DAYS
        if isinstance(payload, dict):
            try:
                window_days = max(1, min(31, int(payload.get("window_days", window_days))))
            except (TypeError, ValueError):
                window_days = self.hse_calendar_service.DEFAULT_WINDOW_DAYS
        explicit_complete = bool(
            isinstance(payload, dict)
            and str(payload.get("snapshot_complete", "")).casefold()
            in {"1", "true", "yes"}
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                normalized_events,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        sync_now = self.time_service.now()
        async with self.session_factory() as session:
            existing_result = await session.execute(
                select(
                    func.count(CalendarEvent.id),
                    func.min(CalendarEvent.start_at),
                    func.max(CalendarEvent.end_at),
                ).where(
                    CalendarEvent.user_id == request["user_id"],
                    CalendarEvent.source == "hse_ios",
                    CalendarEvent.end_at >= sync_now,
                )
            )
            existing_count, _existing_first, _existing_last = existing_result.one()
            first_start = min(
                (self.calendar_service._parse_datetime(item["start"]) for item in normalized_events),
                default=None,
            )
            last_end = max(
                (self.calendar_service._parse_datetime(item["end"]) for item in normalized_events),
                default=None,
            )
            assessment = self.hse_calendar_service.assess_snapshot(
                event_count=len(normalized_events),
                first_start=first_start,
                now=sync_now,
                existing_count=int(existing_count or 0),
                explicit_complete=explicit_complete,
            )
            sync_state = await session.scalar(
                select(CalendarSyncState).where(
                    CalendarSyncState.user_id == request["user_id"],
                    CalendarSyncState.source == "hse_ios",
                )
            )
            has_known_complete = bool(
                int(existing_count or 0) >= 2
                or (
                    sync_state
                    and (
                        sync_state.last_complete_at is not None
                        or (
                            sync_state.integrity_status != "incomplete"
                            and sync_state.last_result in {"updated", "unchanged"}
                        )
                    )
                )
            )
            if (
                sync_state
                and has_known_complete
                and sync_state.last_complete_at is None
            ):
                sync_state.last_complete_at = sync_state.last_success_at
            working_changed = False
            candidate_changed = not sync_state or sync_state.fingerprint != fingerprint
            if assessment.complete and candidate_changed:
                saved = await self.calendar_service.replace_events(
                    session,
                    request["user_id"],
                    {
                        "source": "hse_ios",
                        "replace_all": True,
                        "events": normalized_events,
                    },
                )
                working_changed = True
            elif assessment.complete:
                saved = sync_state.event_count if sync_state else int(existing_count or 0)
            elif not has_known_complete:
                # Quarantine a partial first/legacy snapshot: it is visible in
                # diagnostics, but must not masquerade as a usable schedule.
                if existing_count:
                    await self.calendar_service.replace_events(
                        session,
                        request["user_id"],
                        {
                            "source": "hse_ios",
                            "replace_all": True,
                            "events": [],
                        },
                    )
                    working_changed = True
                saved = 0
            else:
                saved = int(existing_count or 0)
            if sync_state is None:
                sync_state = CalendarSyncState(
                    user_id=request["user_id"],
                    source="hse_ios",
                )
                session.add(sync_state)
            if assessment.complete:
                sync_state.fingerprint = fingerprint
            sync_state.event_count = saved
            sync_state.received_count = len(normalized_events)
            sync_state.window_days = window_days
            sync_state.integrity_status = assessment.status
            sync_state.integrity_reason = assessment.reason
            sync_state.last_attempt_at = sync_now
            sync_state.first_start_at = first_start
            sync_state.last_end_at = last_end
            if assessment.complete:
                sync_state.last_result = "updated" if working_changed else "unchanged"
                sync_state.last_success_at = sync_now
                sync_state.last_complete_at = sync_now
                sync_state.consecutive_failures = 0
                plan = await self._rebuild_plan(
                    session,
                    request["user_id"],
                    sync_now,
                    reason="calendar_synced",
                )
            else:
                sync_state.last_result = "incomplete"
                sync_state.consecutive_failures = int(
                    sync_state.consecutive_failures or 0
                ) + 1
                plan = {"saved": 0, "unallocated": 0, "skipped": True}
            await session.commit()
        logger.info(
            "HSE iPhone calendar synchronized: user=%s saved=%s received=%s "
            "ignored=%s changed=%s integrity=%s",
            request["user_id"],
            saved,
            len(normalized_events),
            ignored,
            working_changed,
            assessment.status,
        )
        return web.json_response({
            "status": "saved" if assessment.complete else "incomplete",
            "calendar": "HSE",
            "events": saved,
            "received": len(normalized_events),
            "ignored": ignored,
            "changed": working_changed,
            "integrity": assessment.status,
            "message": self.hse_calendar_service.integrity_message(
                assessment.status,
                assessment.reason,
                received_count=len(normalized_events),
            ),
            "plan": plan,
            "synced_at": sync_now.isoformat(),
            "first_start": (
                first_start.isoformat() if first_start else None
            ),
            "last_end": (
                last_end.isoformat() if last_end else None
            ),
        })

    @staticmethod
    def _decode_shortcut_object_text(text: str) -> dict | None:
        """Parse the JSON-like object text produced by a Shortcuts dictionary."""
        text = text.strip()
        if not (text.startswith("{") and text.endswith("}")):
            return None
        keys = (
            "id",
            "identifier",
            "идентификатор",
            "title",
            "name",
            "название",
            "имя",
            "start",
            "start_date",
            "начало",
            "дата_начала",
            "end",
            "end_date",
            "окончание",
            "конец",
            "дата_окончания",
            "calendar",
            "calendar_name",
            "календарь",
            "location",
            "геопозиция",
            "местоположение",
            "notes",
            "description",
            "заметки",
            "примечания",
        )
        key_pattern = "|".join(re.escape(key) for key in keys)
        matches = list(
            re.finditer(
                rf'(?:\{{|,)\s*"({key_pattern})"\s*:\s*',
                text,
                flags=re.IGNORECASE,
            )
        )
        if not matches or matches[0].start() != 0:
            return None

        decoded = {}
        for index, match in enumerate(matches):
            value_end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(text) - 1
            )
            raw_value = text[match.end():value_end].strip()
            try:
                value = json.loads(raw_value, strict=False)
            except json.JSONDecodeError:
                if raw_value.startswith('"') and raw_value.endswith('"'):
                    value = raw_value[1:-1]
                    value = value.replace(r'\"', '"').replace(r"\\", "\\")
                else:
                    value = raw_value
            decoded[match.group(1)] = value
        return decoded or None

    @staticmethod
    def _normalized_shortcut_keys(raw: dict) -> dict:
        aliases = {
            "идентификатор": "id",
            "название": "title",
            "имя": "title",
            "начало": "start",
            "дата_начала": "start",
            "окончание": "end",
            "конец": "end",
            "дата_окончания": "end",
            "календарь": "calendar",
            "геопозиция": "location",
            "местоположение": "location",
            "заметки": "notes",
            "примечания": "notes",
        }
        normalized = {}
        for key, value in raw.items():
            name = re.sub(r"[^\w]+", "_", str(key).casefold()).strip("_")
            normalized[aliases.get(name, name)] = value
        return normalized

    @staticmethod
    def _teacher_from_hse_title(title: str) -> str:
        candidate = title.rpartition("·")[2].strip()
        words = candidate.replace("-", " ").split()
        if 2 <= len(words) <= 4 and all(
            word[:1].isupper() and word[1:].islower()
            for word in words
        ):
            return candidate
        return ""

    async def _user_now(self, session, user_id: int):
        profile = await self.user_profile_service.get(session, user_id)
        clock = self.time_service.in_timezone(
            self.user_profile_service.valid_timezone(profile.timezone)
        )
        return clock.now(), profile

    async def _rebuild_plan(self, session, user_id: int, now, *, reason: str) -> dict:
        if self.assistant_loop_service is None:
            return {"saved": 0, "unallocated": 0, "skipped": True}
        try:
            return await self.assistant_loop_service.rebuild_in_session(
                session,
                user_id,
                now,
            )
        except Exception:
            logger.exception(
                "Automatic plan rebuild failed: user=%s reason=%s",
                user_id,
                reason,
            )
            return {"saved": 0, "unallocated": 0, "failed": True}

    @staticmethod
    async def _json_object(request: web.Request) -> dict:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest) as exc:
            raise web.HTTPBadRequest(reason="JSON object expected") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(reason="JSON object expected")
        return payload

    @staticmethod
    def _event_payload(event: CalendarEvent, tz) -> dict:
        return {
            "key": f"event:{event.id}",
            "kind": "class" if event.source in {"hse_ical", "hse_ios"} else "event",
            "title": event.title,
            "start": ensure_aware(event.start_at, tz).isoformat(),
            "end": ensure_aware(event.end_at, tz).isoformat(),
            "meta": event.teacher or event.calendar_name,
            "location": event.location,
            "source": event.source,
        }

    @staticmethod
    def _task_payload(task: Task, now: datetime) -> dict:
        deadline = ensure_aware(task.deadline, now.tzinfo)
        scheduled_start = ensure_aware(task.scheduled_start, now.tzinfo)
        scheduled_end = ensure_aware(task.scheduled_end, now.tzinfo)
        return {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "planning_state": task.planning_state,
            "deadline": deadline.isoformat() if deadline else None,
            "scheduled_start": scheduled_start.isoformat() if scheduled_start else None,
            "scheduled_end": scheduled_end.isoformat() if scheduled_end else None,
            "estimated_minutes": task.estimated_minutes,
            "project": task.project,
            "overdue": bool(deadline and deadline < now and task.status == "active"),
        }

    @staticmethod
    def _conflict_payload(conflict, tz) -> dict:
        return {
            "kind": conflict.kind,
            "first": conflict.first.title,
            "second": conflict.second.title,
            "first_end": ensure_aware(conflict.first.end_at, tz).isoformat(),
            "second_start": ensure_aware(conflict.second.start_at, tz).isoformat(),
            "missing_minutes": conflict.missing_minutes,
            "required_minutes": conflict.required_minutes,
            "approximate": conflict.approximate,
        }

    @staticmethod
    def _task_sort_key(task: Task, now: datetime):
        deadline = ensure_aware(task.deadline, now.tzinfo)
        return (
            0 if deadline and deadline < now else 1,
            deadline or now + timedelta(days=3650),
            -int(task.importance or 0),
            task.id,
        )

    @staticmethod
    def _parse_deadline(value, tz):
        if value in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise web.HTTPBadRequest(reason="invalid deadline") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)

    @staticmethod
    def _bounded_int(value, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(reason="integer value expected") from exc
        if not minimum <= parsed <= maximum:
            raise web.HTTPBadRequest(reason=f"value must be {minimum}–{maximum}")
        return parsed
