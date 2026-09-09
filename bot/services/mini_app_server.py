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
from sqlalchemy import select

from bot.database.models import (
    CalendarEvent,
    Reminder,
    Task,
    TaskPlanBlock,
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
        *,
        calendar_bridge_token: str = "",
        calendar_bridge_owner_id: int | None = None,
        calendar_bridge_public_url: str = "",
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
        app.router.add_post("/api/v1/hse/import", self._import_hse)
        app.router.add_get(
            "/api/v1/hse/shortcut-config",
            self._hse_shortcut_config,
        )
        app.router.add_post(
            "/bridge/v1/hse-calendar",
            self._receive_hse_calendar,
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
            if not self.calendar_bridge_token:
                raise web.HTTPServiceUnavailable(reason="calendar bridge is disabled")
            expected = f"Bearer {self.calendar_bridge_token}"
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
                    self.calendar_bridge_token,
                ) or hmac.compare_digest(body_token, expected)
                if not body_token_valid:
                    raise web.HTTPUnauthorized(reason="invalid calendar bridge token")
                request["bridge_payload"] = payload
            request["user_id"] = self.calendar_bridge_owner_id
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
        response.headers["Cache-Control"] = "public, max-age=300"
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

        serialized_tasks = [self._task_payload(item, now) for item in tasks]
        overdue_count = sum(
            1 for item in active_tasks
            if item.deadline and ensure_aware(item.deadline, now.tzinfo) < now
        )
        hse_count = sum(
            1 for item in events if item.source in {"hse_ical", "hse_ios"}
        )
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
            prefs = self.preferences_service.get_all(profile)
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
                "daily_focus_minutes": prefs["daily_focus_minutes"],
                "workday_start_hour": prefs["workday_start_hour"],
                "workday_end_hour": prefs["workday_end_hour"],
            },
            "hse": {
                "events": hse_status["events"],
                "synced_at": (
                    hse_status["synced_at"].isoformat()
                    if hse_status["synced_at"] else None
                ),
                "periodic_sync": hse_status["configured"],
                "iphone_bridge": bool(
                    self.calendar_bridge_token
                    and user_id == self.calendar_bridge_owner_id
                ),
                "calendar_name": "HSE",
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
                    self._bounded_int(payload["daily_focus_minutes"], 60, 480),
                )
            if "home" in payload:
                try:
                    context = json.loads(profile.preferences_json or "{}")
                except json.JSONDecodeError:
                    context = {}
                context["home"] = " ".join(str(payload["home"] or "").split())[:255]
                profile.preferences_json = json.dumps(context, ensure_ascii=False)
            await session.commit()
        return await self._profile(request)

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
            "window_days": 14,
        })

    async def _receive_hse_calendar(self, request: web.Request) -> web.Response:
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
            try:
                decoded_events = json.loads(raw_events)
            except json.JSONDecodeError:
                decoded_events = None
            if isinstance(decoded_events, (dict, list)):
                raw_events = decoded_events
        # Shortcuts collapses a one-item Repeat Result into the item itself.
        # Treat that dictionary exactly like a one-element event list.
        if isinstance(raw_events, dict):
            raw_events = [raw_events]
        if not isinstance(raw_events, list):
            raise web.HTTPBadRequest(reason="events must be a list or object")
        if len(raw_events) > 2_000:
            raise web.HTTPBadRequest(reason="too many events")

        normalized_events = []
        ignored = 0
        for raw in raw_events:
            if not isinstance(raw, dict):
                ignored += 1
                continue
            values = self._normalized_shortcut_keys(raw)
            calendar_name = str(
                values.get("calendar") or values.get("calendar_name") or ""
            ).strip()
            if calendar_name.casefold() != "hse":
                ignored += 1
                continue
            title = " ".join(str(values.get("title") or "").split())
            start = values.get("start") or values.get("start_date")
            end = values.get("end") or values.get("end_date")
            if not title or not start or not end:
                ignored += 1
                continue
            try:
                start_at = self.calendar_service._parse_datetime(start)
                end_at = self.calendar_service._parse_datetime(end)
            except (TypeError, ValueError):
                ignored += 1
                continue
            if end_at <= start_at:
                ignored += 1
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
            raise web.HTTPBadRequest(
                reason="no valid events from the HSE calendar"
            )
        async with self.session_factory() as session:
            saved = await self.calendar_service.replace_events(
                session,
                request["user_id"],
                {
                    "source": "hse_ios",
                    "replace_all": True,
                    "events": normalized_events,
                },
            )
            await session.commit()
        logger.info(
            "HSE iPhone calendar synchronized: user=%s saved=%s ignored=%s",
            request["user_id"],
            saved,
            ignored,
        )
        return web.json_response({
            "status": "saved",
            "calendar": "HSE",
            "events": saved,
            "ignored": ignored,
        })

    @staticmethod
    def _normalized_shortcut_keys(raw: dict) -> dict:
        return {
            re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_"): value
            for key, value in raw.items()
        }

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
