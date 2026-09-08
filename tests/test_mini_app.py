from __future__ import annotations

import hashlib
import hmac
import json
import time as time_module
import unittest
from datetime import datetime, time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base
from bot.services.calendar_service import CalendarService
from bot.services.conflict_service import ConflictService
from bot.services.hse_calendar_service import HSECalendarService
from bot.services.mini_app_server import (
    MiniAppAuthError,
    MiniAppServer,
    TelegramInitDataValidator,
)
from bot.services.time_service import TimeService
from bot.services.user_profile_service import UserProfileService


class FixedTimeService(TimeService):
    def now(self):
        return datetime(2026, 9, 9, 10, 30, tzinfo=self.tz)


def signed_init_data(token: str, user_id: int, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(
            {"id": user_id, "first_name": "Test"},
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret,
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(fields)


class TelegramInitDataValidatorTests(unittest.TestCase):
    def test_accepts_signed_allowed_user(self):
        now = int(time_module.time())
        validator = TelegramInitDataValidator("123:secret", [42])

        self.assertEqual(
            42,
            validator.validate(
                signed_init_data("123:secret", 42, now),
                now_timestamp=now,
            ),
        )

    def test_rejects_tampered_expired_and_unlisted_data(self):
        now = int(time_module.time())
        validator = TelegramInitDataValidator("123:secret", [42], max_age_seconds=60)
        valid = signed_init_data("123:secret", 42, now)

        with self.assertRaises(MiniAppAuthError):
            validator.validate(valid.replace("Test", "Other"), now_timestamp=now)
        with self.assertRaises(MiniAppAuthError):
            validator.validate(
                signed_init_data("123:secret", 42, now - 61),
                now_timestamp=now,
            )
        with self.assertRaises(MiniAppAuthError):
            validator.validate(
                signed_init_data("123:secret", 99, now),
                now_timestamp=now,
            )


class MiniAppAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.clock = FixedTimeService(
            ZoneInfo("Europe/Moscow"),
            time(9),
            time(14),
            time(19),
            time(22),
        )
        calendar = CalendarService(self.clock)
        hse = HSECalendarService(calendar, self.sessions, self.clock, user_id=42)
        self.service = MiniAppServer(
            "127.0.0.1",
            0,
            "123:secret",
            [42, 43],
            self.sessions,
            self.clock,
            UserProfileService("Europe/Moscow"),
            calendar,
            ConflictService(),
            hse,
            dev_mode=True,
        )
        self.client = TestClient(TestServer(self.service.create_app()))
        await self.client.start_server()
        self.headers = {"X-Debug-User": "42"}

    async def asyncTearDown(self):
        await self.client.close()
        await self.engine.dispose()

    async def test_static_app_has_persistent_navigation_and_security_headers(self):
        response = await self.client.get("/")
        html = await response.text()

        self.assertEqual(200, response.status)
        self.assertIn('class="bottom-nav"', html)
        self.assertIn('data-view="planner"', html)
        self.assertIn('data-view="tasks"', html)
        self.assertIn("https://telegram.org/js/telegram-web-app.js", html)
        self.assertIn("frame-ancestors", response.headers["Content-Security-Policy"])

    async def test_api_requires_telegram_authorization(self):
        response = await self.client.get("/api/v1/state")

        self.assertEqual(401, response.status)
        self.assertIn("Telegram", (await response.json())["error"])

    async def test_task_completion_is_visible_and_can_be_undone(self):
        created_response = await self.client.post(
            "/api/v1/tasks",
            headers=self.headers,
            json={"title": "Отправить документы", "estimated_minutes": 30},
        )
        created = await created_response.json()

        self.assertEqual(201, created_response.status)
        complete_response = await self.client.patch(
            f"/api/v1/tasks/{created['id']}",
            headers=self.headers,
            json={"status": "completed"},
        )
        self.assertEqual(200, complete_response.status)

        state_response = await self.client.get(
            "/api/v1/state?period=today",
            headers=self.headers,
        )
        state = await state_response.json()
        completed = [task for task in state["tasks"] if task["status"] == "completed"]
        self.assertEqual([created["id"]], [task["id"] for task in completed])

        undo_response = await self.client.patch(
            f"/api/v1/tasks/{created['id']}",
            headers=self.headers,
            json={"status": "active"},
        )
        self.assertEqual("active", (await undo_response.json())["status"])

    async def test_task_mutation_is_scoped_to_authenticated_user(self):
        created = await (
            await self.client.post(
                "/api/v1/tasks",
                headers=self.headers,
                json={"title": "Личная задача"},
            )
        ).json()

        response = await self.client.patch(
            f"/api/v1/tasks/{created['id']}",
            headers={"X-Debug-User": "43"},
            json={"status": "completed"},
        )

        self.assertEqual(404, response.status)

    async def test_profile_settings_round_trip(self):
        response = await self.client.patch(
            "/api/v1/profile",
            headers=self.headers,
            json={
                "timezone": "Asia/Almaty",
                "home": "Хамовники",
                "daily_focus_minutes": 240,
            },
        )
        profile = await response.json()

        self.assertEqual(200, response.status)
        self.assertEqual("Asia/Almaty", profile["timezone"])
        self.assertEqual("Хамовники", profile["home"])
        self.assertEqual(240, profile["planning"]["daily_focus_minutes"])
