from __future__ import annotations

import hashlib
import hmac
import json
import time as time_module
import unittest
from datetime import date, datetime, time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import (
    Base,
    CalendarEvent,
    DailyWellnessLog,
    HealthSnapshot,
    TrainingProfile,
    WorkoutSession,
)
from bot.services.calendar_service import CalendarService
from bot.services.conflict_service import ConflictService
from bot.services.health_service import HealthService
from bot.services.hse_calendar_service import HSECalendarService
from bot.services.mini_app_server import (
    MiniAppAuthError,
    MiniAppServer,
    TelegramInitDataValidator,
)
from bot.services.time_service import TimeService
from bot.services.training_service import TrainingService
from bot.services.user_profile_service import UserProfileService

CALENDAR_TOKEN = "calendar-secret-calendar-secret-1234"
HEALTH_TOKEN = "health-secret-health-secret-health-1234"


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
        health = HealthService(
            None,
            self.sessions,
            self.clock,
            None,
            42,
            True,
            time(18),
        )
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
            health,
            TrainingService(calendar),
            calendar_bridge_token=CALENDAR_TOKEN,
            calendar_bridge_owner_id=42,
            calendar_bridge_public_url=(
                "https://assistant.example.com/assistant/bridge/v1/hse-calendar"
            ),
            health_bridge_token=HEALTH_TOKEN,
            health_bridge_owner_id=42,
            health_bridge_public_url=(
                "https://assistant.example.com/assistant/bridge/v1/health-snapshot"
            ),
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
        self.assertIn('data-view="health"', html)
        self.assertIn("assets/resource_store.js", html)
        self.assertIn("https://telegram.org/js/telegram-web-app.js", html)
        self.assertIn("frame-ancestors", response.headers["Content-Security-Policy"])

        resource_response = await self.client.get("/assets/resource_store.js")
        self.assertEqual(200, resource_response.status)

    async def test_empty_today_is_a_successful_ready_state(self):
        response = await self.client.get(
            "/api/v1/state?period=today",
            headers=self.headers,
        )
        payload = await response.json()

        self.assertEqual(200, response.status)
        self.assertEqual([], payload["timeline"])
        self.assertEqual(0, payload["stats"]["events"])

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
                "daily_focus_minutes": 1440,
            },
        )
        profile = await response.json()

        self.assertEqual(200, response.status)
        self.assertEqual("Asia/Almaty", profile["timezone"])
        self.assertEqual("Хамовники", profile["home"])
        self.assertEqual(1440, profile["planning"]["daily_focus_minutes"])
        self.assertEqual(480, profile["planning"]["auto_planning_minutes"])
        self.assertEqual("overload", profile["planning"]["focus_load_level"])
        self.assertIn("сон", profile["planning"]["focus_warning"])

    async def test_shortcut_config_is_visible_only_to_owner(self):
        response = await self.client.get(
            "/api/v1/hse/shortcut-config",
            headers=self.headers,
        )
        config = await response.json()

        self.assertEqual(200, response.status)
        self.assertEqual("HSE", config["calendar"])
        self.assertEqual(f"Bearer {CALENDAR_TOKEN}", config["authorization"])
        self.assertEqual(CALENDAR_TOKEN, config["token"])
        self.assertEqual("07:00", config["daily_trigger"])
        self.assertTrue(config["run_immediately"])
        self.assertTrue(config["silent"])
        forbidden = await self.client.get(
            "/api/v1/hse/shortcut-config",
            headers={"X-Debug-User": "43"},
        )
        self.assertEqual(403, forbidden.status)

    async def test_hse_calendar_bridge_filters_normalizes_and_clears_snapshot(self):
        unauthorized = await self.client.post(
            "/bridge/v1/hse-calendar",
            json={"events": []},
        )
        self.assertEqual(401, unauthorized.status)

        wrong_body_token = await self.client.post(
            "/bridge/v1/hse-calendar",
            json={"token": "wrong", "events": []},
        )
        self.assertEqual(401, wrong_body_token.status)

        body_authorized = await self.client.post(
            "/bridge/v1/hse-calendar",
            json={
                "token": CALENDAR_TOKEN,
                "events": {
                    "id": "single-ios-event",
                    "title": "Единственная пара",
                    "start": "2026-09-09T10:00:00+03:00",
                    "end": "2026-09-09T11:20:00+03:00",
                    "calendar": "HSE",
                },
            },
        )
        self.assertEqual(200, body_authorized.status)
        self.assertEqual(1, (await body_authorized.json())["events"])

        text_authorized = await self.client.post(
            "/bridge/v1/hse-calendar",
            json={
                "token": CALENDAR_TOKEN,
                "events": json.dumps({
                    "id": "stringified-ios-event",
                    "title": "Событие из текстового JSON",
                    "start": "2026-09-09T11:30:00+03:00",
                    "end": "2026-09-09T12:50:00+03:00",
                    "calendar": "HSE",
                }),
            },
        )
        self.assertEqual(200, text_authorized.status)
        self.assertEqual(1, (await text_authorized.json())["events"])

        nested_array_authorized = await self.client.post(
            "/bridge/v1/hse-calendar",
            json={
                "token": CALENDAR_TOKEN,
                "events": [[{
                    "идентификатор": "nested-ios-event",
                    "название": "Событие из вложенного массива Shortcuts",
                    "начало": "2026-09-09T13:00:00+03:00",
                    "окончание": "2026-09-09T14:20:00+03:00",
                    "календарь": "HSE",
                }]],
            },
        )
        self.assertEqual(200, nested_array_authorized.status)
        self.assertEqual(1, (await nested_array_authorized.json())["events"])

        escaped_item_authorized = await self.client.post(
            "/bridge/v1/hse-calendar",
            json={
                "token": CALENDAR_TOKEN,
                "events": [[
                    r'{\"id\":\"escaped-ios-event\",'
                    r'\"title\":\"CSP \\\"Consultant\\\"\",'
                    r'\"start\":\"2026-09-10T13:00:00+03:00\",'
                    r'\"end\":\"2026-09-10T14:20:00+03:00\",'
                    r'\"calendar\":\"HSE\"}'
                ]],
            },
        )
        self.assertEqual(200, escaped_item_authorized.status)
        self.assertEqual(1, (await escaped_item_authorized.json())["events"])

        json_like_item_authorized = await self.client.post(
            "/bridge/v1/hse-calendar",
            json={
                "token": CALENDAR_TOKEN,
                "events": [[
                    r'{"end":"2026-09-10T14:20:00+03:00",'
                    r'"location":"519, Б. Трехсвятительский пер., д. 3",'
                    r'"start":"2026-09-10T13:00:00+03:00",'
                    r'"id":"json-like-event","notes":"",'
                    r'"title":"СПС "Консультант Плюс"",'
                    r'"calendar":"HSE"}'
                ]],
            },
        )
        self.assertEqual(200, json_like_item_authorized.status)
        self.assertEqual(1, (await json_like_item_authorized.json())["events"])

        invalid_text = await self.client.post(
            "/bridge/v1/hse-calendar",
            json={"token": CALENDAR_TOKEN, "events": [["1 object"]]},
        )
        self.assertEqual(400, invalid_text.status)
        self.assertIn("text=1 object", (await invalid_text.json())["error"])

        response = await self.client.post(
            "/bridge/v1/hse-calendar",
            headers={"Authorization": f"Bearer {CALENDAR_TOKEN}"},
            json={
                "events": [
                    {
                        "id": "ios-event-1",
                        "title": (
                            "Проектный семинар · Научно-исследовательский "
                            "семинар · Солдаткина Оксана Леонидовна"
                        ),
                        "start": "2026-09-09T13:00:00+03:00",
                        "end": "2026-09-09T14:20:00+03:00",
                        "calendar": "HSE",
                        "location": "435, Б. Трехсвятительский пер., д. 3",
                        "notes": "24e7c7759ccca9305959285074dd9f20eb3ff",
                    },
                    {
                        "id": "personal-event",
                        "title": "Личное событие",
                        "start": "2026-09-09T15:00:00+03:00",
                        "end": "2026-09-09T16:00:00+03:00",
                        "calendar": "Личный",
                    },
                ]
            },
        )
        result = await response.json()

        self.assertEqual(200, response.status)
        self.assertEqual(1, result["events"])
        self.assertEqual(1, result["ignored"])
        self.assertTrue(result["changed"])
        async with self.sessions() as session:
            events = list(
                (await session.execute(select(CalendarEvent))).scalars().all()
            )
            first_event_id = events[0].id
        self.assertEqual(1, len(events))
        self.assertEqual("hse_ios", events[0].source)
        self.assertEqual("HSE", events[0].calendar_name)
        self.assertEqual("435", events[0].room)
        self.assertIn("Трехсвятительский", events[0].building)
        self.assertEqual("Солдаткина Оксана Леонидовна", events[0].teacher)

        unchanged = await self.client.post(
            "/bridge/v1/hse-calendar",
            headers={"Authorization": f"Bearer {CALENDAR_TOKEN}"},
            json={"events": [{
                "id": "ios-event-1",
                "title": (
                    "Проектный семинар · Научно-исследовательский "
                    "семинар · Солдаткина Оксана Леонидовна"
                ),
                "start": "2026-09-09T13:00:00+03:00",
                "end": "2026-09-09T14:20:00+03:00",
                "calendar": "HSE",
                "location": "435, Б. Трехсвятительский пер., д. 3",
                "notes": "24e7c7759ccca9305959285074dd9f20eb3ff",
            }]},
        )
        unchanged_result = await unchanged.json()
        self.assertEqual(200, unchanged.status)
        self.assertFalse(unchanged_result["changed"])
        async with self.sessions() as session:
            unchanged_event = await session.scalar(select(CalendarEvent))
        self.assertEqual(first_event_id, unchanged_event.id)

        async with self.sessions() as session:
            session.add(CalendarEvent(
                user_id=42,
                external_id="personal-existing",
                calendar_name="Личный",
                title="Не удалять",
                start_at=datetime(
                    2026, 9, 9, 18, 0, tzinfo=ZoneInfo("Europe/Moscow")
                ),
                end_at=datetime(
                    2026, 9, 9, 19, 0, tzinfo=ZoneInfo("Europe/Moscow")
                ),
                source="shortcut",
            ))
            await session.commit()

        rejected = await self.client.post(
            "/bridge/v1/hse-calendar",
            headers={"Authorization": f"Bearer {CALENDAR_TOKEN}"},
            json={
                "events": [
                    {
                        "title": "Личное событие",
                        "start": "2026-09-09T15:00:00+03:00",
                        "end": "2026-09-09T16:00:00+03:00",
                        "calendar": "Личный",
                    }
                ]
            },
        )
        self.assertEqual(400, rejected.status)
        async with self.sessions() as session:
            preserved = list(
                (await session.execute(select(CalendarEvent))).scalars().all()
            )
        self.assertEqual(2, len(preserved))

        cleared = await self.client.post(
            "/bridge/v1/hse-calendar",
            headers={"Authorization": f"Bearer {CALENDAR_TOKEN}"},
            json={"events": []},
        )
        async with self.sessions() as session:
            remaining = list(
                (await session.execute(select(CalendarEvent))).scalars().all()
            )
        self.assertEqual(200, cleared.status)
        self.assertEqual(["shortcut"], [event.source for event in remaining])
        cleared_result = await cleared.json()
        self.assertTrue(cleared_result["changed"])

        profile_response = await self.client.get(
            "/api/v1/profile",
            headers=self.headers,
        )
        profile = await profile_response.json()
        self.assertEqual(0, profile["hse"]["events"])
        self.assertEqual("fresh", profile["hse"]["sync_status"])
        self.assertEqual("updated", profile["hse"]["last_result"])
        self.assertIsNotNone(profile["hse"]["synced_at"])

    async def test_empty_health_is_ready_and_intake_is_independent(self):
        empty_response = await self.client.get(
            "/api/v1/health",
            headers=self.headers,
        )
        empty = await empty_response.json()

        self.assertEqual(200, empty_response.status)
        self.assertFalse(empty["apple_health"]["connected"])
        self.assertEqual("unknown", empty["recovery"]["level"])
        self.assertEqual([], empty["training"]["upcoming"])

        goals_response = await self.client.patch(
            "/api/v1/health/goals",
            headers=self.headers,
            json={"water_ml": 2500, "protein_g": 140},
        )
        goals = await goals_response.json()
        self.assertEqual(200, goals_response.status)
        self.assertEqual({"water_ml": 2500, "protein_g": 140}, goals["goals"])

        intake_response = await self.client.post(
            "/api/v1/health/intake",
            headers=self.headers,
            json={"water_ml": 250, "protein_g": 20},
        )
        intake = await intake_response.json()
        self.assertEqual(200, intake_response.status)
        self.assertEqual(250, intake["intake"]["water_ml"])
        self.assertEqual(20, intake["intake"]["protein_g"])
        async with self.sessions() as session:
            daily_log = await session.scalar(select(DailyWellnessLog))
        self.assertEqual(date(2026, 9, 9), daily_log.date)

    async def test_health_bridge_recovery_and_training_dashboard(self):
        unauthorized = await self.client.post(
            "/bridge/v1/health-snapshot",
            json={"date": "2026-09-09", "steps": 1000},
        )
        self.assertEqual(401, unauthorized.status)

        saved_response = await self.client.post(
            "/bridge/v1/health-snapshot",
            json={
                "token": HEALTH_TOKEN,
                "date": "2026-09-09",
                "steps": 3200,
                "step_goal": 9000,
                "sleep_minutes": 330,
                "workout_minutes": 0,
                "active_energy_kcal": 180,
                "resting_heart_rate": 58,
                "hrv_ms": 47,
            },
        )
        saved = await saved_response.json()
        self.assertEqual(200, saved_response.status)
        self.assertEqual("saved", saved["status"])
        self.assertEqual(330, saved["sleep_minutes"])

        async with self.sessions() as session:
            profile = TrainingProfile(
                user_id=42,
                goal="Прогрессировать в базовых упражнениях",
                active=True,
            )
            session.add(profile)
            session.add_all([
                WorkoutSession(
                    user_id=42,
                    title="Силовая A",
                    scheduled_for=datetime(
                        2026, 9, 10, 18, 0,
                        tzinfo=ZoneInfo("Europe/Moscow"),
                    ),
                    status="planned",
                    details_json=json.dumps({
                        "estimated_minutes": 55,
                        "focus": "Ноги и жим",
                        "exercises": [{
                            "name": "Присед",
                            "sets": 3,
                            "reps": "6–8",
                            "progression_note": "Добавь один повтор при чистой технике.",
                        }],
                    }, ensure_ascii=False),
                ),
                WorkoutSession(
                    user_id=42,
                    title="Силовая B",
                    scheduled_for=datetime(
                        2026, 9, 7, 18, 0,
                        tzinfo=ZoneInfo("Europe/Moscow"),
                    ),
                    completed_at=datetime(
                        2026, 9, 7, 19, 0,
                        tzinfo=ZoneInfo("Europe/Moscow"),
                    ),
                    status="completed",
                    rpe=9,
                    notes="Тяжело",
                ),
            ])
            await session.commit()

        dashboard_response = await self.client.get(
            "/api/v1/health",
            headers=self.headers,
        )
        dashboard = await dashboard_response.json()
        self.assertEqual(200, dashboard_response.status)
        self.assertTrue(dashboard["apple_health"]["connected"])
        self.assertEqual("overload", dashboard["recovery"]["level"])
        self.assertIn("восстановление", dashboard["recovery"]["title"].lower())
        self.assertEqual("Силовая A", dashboard["training"]["upcoming"][0]["title"])
        self.assertEqual("Силовая B", dashboard["training"]["history"][0]["title"])
        self.assertEqual(
            "recovery",
            dashboard["training"]["micro_plan"]["level"],
        )

        config_response = await self.client.get(
            "/api/v1/health/shortcut-config",
            headers=self.headers,
        )
        config = await config_response.json()
        self.assertEqual(200, config_response.status)
        self.assertEqual(HEALTH_TOKEN, config["token"])
        self.assertIn("sleep_minutes", config["fields"])

        async with self.sessions() as session:
            snapshots = list(
                (await session.execute(select(HealthSnapshot))).scalars().all()
            )
        self.assertEqual(1, len(snapshots))
