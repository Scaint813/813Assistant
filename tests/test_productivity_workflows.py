from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, CalendarEvent, HealthSnapshot, Task
from bot.services.ai_service import AIService
from bot.services.assistant_ux_service import AssistantUXService
from bot.services.calendar_service import CalendarService
from bot.services.focus_service import FocusService
from bot.services.health_service import HealthService
from bot.services.intent_parser import IntentParser
from bot.services.time_service import TimeService
from bot.services.training_service import TrainingService


class FixedTimeService(TimeService):
    def now(self):
        return datetime(2026, 8, 3, 9, 0, tzinfo=self.tz)


class ProductivityWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tz = ZoneInfo("Europe/Moscow")
        self.clock = FixedTimeService(self.tz, time(9), time(14), time(19), time(22))
        self.calendar = CalendarService(self.clock)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_unqualified_task_goes_to_inbox(self):
        parser = IntentParser(AIService("", "", ""), self.clock)
        parsed = await parser.parse_user_text("Подготовить документы", {})
        task = parsed["intents"][0]
        self.assertEqual("inbox", task["planning_state"])
        self.assertFalse(task["duration_confirmed"])
        self.assertFalse(task["deadline_confirmed"])

    async def test_explicit_duration_and_date_make_task_ready(self):
        parser = IntentParser(AIService("", "", ""), self.clock)
        parsed = await parser.parse_user_text(
            "Подготовить документы завтра на 45 минут", {}
        )
        task = parsed["intents"][0]
        self.assertEqual("ready", task["planning_state"])
        self.assertEqual(45, task["estimated_minutes"])
        self.assertTrue(task["deadline"].startswith("2026-08-04"))

    async def test_calendar_plan_respects_busy_event_and_capacity(self):
        now = self.clock.now()
        async with self.sessions() as session:
            session.add(CalendarEvent(
                user_id=1, external_id="meeting", title="Встреча",
                start_at=now.replace(hour=10), end_at=now.replace(hour=11),
            ))
            session.add_all([
                Task(
                    user_id=1, title="Подготовить отчёт", planning_state="ready",
                    estimated_minutes=60, duration_confirmed=True, deadline_confirmed=True,
                    deadline=now.replace(hour=18),
                ),
                Task(
                    user_id=1, title="Неполная задача", planning_state="inbox",
                    estimated_minutes=30,
                ),
            ])
            await session.commit()
            plan = await self.calendar.build_day_plan(session, 1, now)
            await session.commit()
        self.assertEqual(1, len(plan["timeboxes"]))
        self.assertEqual(1, len(plan["inbox"]))
        block = plan["timeboxes"][0]
        self.assertFalse(block["start"] < now.replace(hour=11) and block["end"] > now.replace(hour=10))

    async def test_large_task_gets_a_bounded_block_and_multi_day_forecast(self):
        now = self.clock.now()
        async with self.sessions() as session:
            session.add(Task(
                user_id=1,
                title="Большой запуск",
                next_action="Собрать требования",
                planning_state="ready",
                estimated_minutes=2400,
                duration_confirmed=True,
                deadline_confirmed=True,
                deadline=now + timedelta(days=7),
            ))
            await session.commit()
            plan = await self.calendar.build_day_plan(session, 1, now)
            screen = await AssistantUXService(
                calendar_service=self.calendar
            ).plan_day(session, 1, now)

        self.assertEqual(1, len(plan["timeboxes"]))
        self.assertEqual(90, plan["timeboxes"][0]["planned_minutes"])
        self.assertEqual(2400, plan["timeboxes"][0]["total_minutes"])
        self.assertEqual(2310, plan["timeboxes"][0]["remaining_minutes"])
        self.assertGreater(plan["oversized"][0]["estimated_days"], 1)
        self.assertIn("Сегодня только 1 ч 30 мин из 40 ч", screen.text)
        self.assertIn("Большие задачи — не обещаю закрыть за день", screen.text)
        self.assertIn("Europe/Moscow", screen.text)

    async def test_focus_completion_closes_real_task(self):
        now = self.clock.now()
        service = FocusService()
        async with self.sessions() as session:
            task = Task(user_id=1, title="Отправить документы", planning_state="ready")
            session.add(task)
            await session.flush()
            focus = await service.start(session, 1, task.id, now, 25)
            await service.finish(session, 1, now + timedelta(minutes=20), "completed")
            await session.commit()
            self.assertEqual("completed", focus.status)
            self.assertEqual("done", task.status)

    async def test_health_stores_recovery_signals(self):
        service = HealthService(
            scheduler=None, session_factory=self.sessions, time_service=self.clock,
            bot=None, user_id=1, enabled=False, nudge_time=time(18),
        )
        async with self.sessions() as session:
            row = await service.upsert_snapshot(session, 1, {
                "date": date(2026, 8, 3), "steps": 5000,
                "resting_heart_rate": 61, "hrv_ms": 52,
            })
            await session.commit()
        self.assertEqual(61, row.resting_heart_rate)
        self.assertEqual(52, row.hrv_ms)

    async def test_training_reduces_volume_on_clear_recovery_signal(self):
        service = TrainingService()
        now = self.clock.now()
        async with self.sessions() as session:
            for days_ago in range(1, 6):
                session.add(HealthSnapshot(
                    user_id=1, date=now.date() - timedelta(days=days_ago),
                    sleep_minutes=450, resting_heart_rate=55, hrv_ms=60,
                ))
            session.add(HealthSnapshot(
                user_id=1, date=now.date(), sleep_minutes=300,
                resting_heart_rate=70, hrv_ms=35,
            ))
            await session.commit()
            modifier, reason = await service._adjustment(session, 1, now)
        self.assertEqual(0.8, modifier)
        self.assertIn("Восстановительный объём", reason)


if __name__ == "__main__":
    unittest.main()
