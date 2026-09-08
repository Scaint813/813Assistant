from __future__ import annotations

import unittest
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, CalendarEvent, Reminder, Task, UserProfile
from bot.services.calendar_service import CalendarService
from bot.services.conflict_service import ConflictService, RouteTimeEstimator
from bot.services.daily_brief_service import DailyBriefService
from bot.services.hse_calendar_service import HSECalendarService
from bot.services.time_service import TimeService


class FixedTimeService(TimeService):
    def now(self):
        return datetime(2026, 9, 8, 8, 0, tzinfo=self.tz)


ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//HSE//RUZ//RU
BEGIN:VEVENT
UID:law-1
DTSTART;TZID=Europe/Moscow:20260908T144000
DTEND;TZID=Europe/Moscow:20260908T160000
SUMMARY:Лекция — Теория государства и права
DESCRIPTION:Преподаватель: Иванов Иван Иванович
LOCATION:Б. Трехсвятительский пер., 3, ауд. 519
END:VEVENT
END:VCALENDAR
"""


class AcademicIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tz = ZoneInfo("Europe/Moscow")
        self.clock = FixedTimeService(
            self.tz, time(9), time(14), time(19), time(22)
        )
        self.calendar = CalendarService(self.clock)
        self.hse = HSECalendarService(
            self.calendar, self.sessions, self.clock, user_id=1
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_ical_import_normalizes_and_replaces_snapshot(self):
        first = await self.hse.import_bytes(ICS)
        changed = ICS.replace("T144000", "T154000").replace("T160000", "T170000")
        second = await self.hse.import_bytes(changed)

        async with self.sessions() as session:
            count = await session.scalar(select(func.count(CalendarEvent.id)))
            event = (await session.execute(select(CalendarEvent))).scalar_one()

        self.assertEqual(1, first.imported)
        self.assertEqual(1, second.imported)
        self.assertEqual(1, count)
        self.assertEqual("hse_ical:law-1", event.external_id)
        self.assertEqual("lecture", event.event_type)
        self.assertEqual("Иванов Иван Иванович", event.teacher)
        self.assertEqual("519", event.room)
        self.assertIn("Трехсвятительский", event.building)
        self.assertEqual(15, event.start_at.hour)

    def test_feed_url_is_restricted_to_hse_https(self):
        self.assertEqual(
            "https://ruz.hse.ru/feed/private.ics",
            self.hse._validated_url("webcal://ruz.hse.ru/feed/private.ics"),
        )
        with self.assertRaises(ValueError):
            self.hse._validated_url("http://ruz.hse.ru/feed.ics")
        with self.assertRaises(ValueError):
            self.hse._validated_url("https://hse.ru.example.org/feed.ics")

    def test_conflicts_include_route_buffer_and_ignore_duplicate_mirrors(self):
        first = CalendarEvent(
            id=1,
            user_id=1,
            external_id="first",
            title="Семинар",
            location="Дубки",
            source="hse_ical",
            start_at=self.clock.now().replace(hour=10),
            end_at=self.clock.now().replace(hour=11),
        )
        duplicate = CalendarEvent(
            id=2,
            user_id=1,
            external_id="mirror",
            title="Семинар",
            location="Дубки",
            source="apple_calendar",
            start_at=self.clock.now().replace(hour=10),
            end_at=self.clock.now().replace(hour=11),
        )
        second = CalendarEvent(
            id=3,
            user_id=1,
            external_id="second",
            title="Встреча",
            location="Покровка",
            source="apple_calendar",
            start_at=self.clock.now().replace(hour=11, minute=30),
            end_at=self.clock.now().replace(hour=12, minute=30),
        )
        context = {
            "route_minutes": {"дубки->покровка": 60},
            "rules": {"arrival_buffer_minutes": 15},
        }

        conflicts = ConflictService().detect(
            [first, duplicate, second], self.tz, context
        )

        self.assertEqual(1, len(conflicts))
        self.assertEqual("transfer", conflicts[0].kind)
        self.assertEqual(75, conflicts[0].required_minutes)
        self.assertEqual(45, conflicts[0].missing_minutes)
        self.assertFalse(conflicts[0].approximate)

    def test_contained_event_reports_actual_overlap_length(self):
        now = self.clock.now()
        outer = CalendarEvent(
            id=1,
            user_id=1,
            external_id="outer",
            title="Длинная встреча",
            start_at=now.replace(hour=9),
            end_at=now.replace(hour=12),
        )
        inner = CalendarEvent(
            id=2,
            user_id=1,
            external_id="inner",
            title="Короткая встреча",
            start_at=now.replace(hour=10),
            end_at=now.replace(hour=11),
        )

        conflict = ConflictService().detect([outer, inner], self.tz)[0]

        self.assertEqual(60, conflict.missing_minutes)

    async def test_daily_brief_is_grounded_and_does_not_reschedule_tasks(self):
        await self.hse.import_bytes(ICS)
        async with self.sessions() as session:
            profile = UserProfile(
                user_id=1,
                preferences_json=(
                    '{"home":"Дубки","route_minutes":'
                    '{"дубки->б трехсвятительский пер 3 ауд 519":60}}'
                ),
            )
            task = Task(
                user_id=1,
                title="Сдать эссе",
                planning_state="ready",
                estimated_minutes=60,
                duration_confirmed=True,
                deadline=self.clock.now().replace(hour=19),
            )
            session.add_all([
                profile,
                task,
                Reminder(
                    user_id=1,
                    text="Позвонить деканату",
                    remind_at=self.clock.now().replace(hour=13),
                ),
                CalendarEvent(
                    user_id=1,
                    external_id="overlap",
                    source="personal",
                    title="Созвон",
                    location="Онлайн",
                    start_at=self.clock.now().replace(hour=15, minute=30),
                    end_at=self.clock.now().replace(hour=16, minute=30),
                ),
            ])
            await session.commit()

            service = DailyBriefService(
                self.calendar,
                ConflictService(RouteTimeEstimator()),
            )
            brief = await service.build(session, 1, self.clock.now())
            await session.refresh(task)

        self.assertIn("Сводка дня", brief)
        self.assertIn("Теория государства и права", brief)
        self.assertIn("Выйти из дома", brief)
        self.assertIn("Сдать эссе", brief)
        self.assertIn("Позвонить деканату", brief)
        self.assertIn("пересечение", brief)
        self.assertIsNone(task.scheduled_start)

    async def test_conflict_query_reads_database(self):
        now = self.clock.now()
        async with self.sessions() as session:
            session.add_all([
                CalendarEvent(
                    user_id=1,
                    external_id="a",
                    title="A",
                    start_at=now.replace(hour=9),
                    end_at=now.replace(hour=10),
                ),
                CalendarEvent(
                    user_id=1,
                    external_id="b",
                    title="B",
                    start_at=now.replace(hour=9, minute=45),
                    end_at=now.replace(hour=10, minute=30),
                ),
            ])
            await session.commit()
            conflicts = await ConflictService().detect_between(
                session,
                1,
                now.replace(hour=0),
                now.replace(hour=0) + timedelta(days=1),
            )
        self.assertEqual("overlap", conflicts[0].kind)
        self.assertEqual(15, conflicts[0].missing_minutes)
