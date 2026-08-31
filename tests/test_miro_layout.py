from __future__ import annotations

import unittest
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, MiroMapping, ProblemBlock, Reminder, ScheduleOverride, Task
from bot.services.miro_service import FRAME_H, SHAPE_W, MiroService
from bot.services.time_service import TimeService


class RecordingMiroService(MiroService):
    def __init__(self):
        super().__init__("token", "board", 5000, 0)
        self.requests = []

    async def _safe_request(self, method, url, payload, entity_type, entity_id, stats):
        self.requests.append((method, url, payload, entity_type, entity_id))
        if method == "patch":
            return None, "updated"
        return f"created-{len(self.requests)}", "created"

    async def _delete_item_quietly(self, item_id):
        return True


class FixedTimeService(TimeService):
    def now(self):
        return datetime(2026, 7, 31, 12, 0, tzinfo=self.tz)


class MiroLayoutTests(unittest.IsolatedAsyncioTestCase):
    def test_frames_never_cross_manual_zone(self):
        service = RecordingMiroService()

        for key in ("today", "tasks", "reminders", "problems", "schedule", "archive"):
            x, _ = service._section_xy(key)
            self.assertGreaterEqual(x - SHAPE_W // 2, service.start_x)

    async def test_cards_are_real_cards_with_parent_relative_coordinates(self):
        service = RecordingMiroService()
        frame_x, frame_y = service._section_xy("tasks")
        service._frame_positions["frame-1"] = (frame_x, frame_y)
        stats = {"created": 0, "updated": 0, "errors": 0}

        await service._create_or_update_card(
            "",
            "Поход к врачу",
            "Приоритет: high",
            frame_x,
            frame_y - FRAME_H // 2 + 300,
            "#d32f2f",
            "task",
            42,
            stats,
            "frame-1",
        )

        method, url, payload, _, _ = service.requests[0]
        self.assertEqual("post", method)
        self.assertTrue(url.endswith("/cards"))
        self.assertEqual({"id": "frame-1"}, payload["parent"])
        self.assertEqual("parent_top_left", payload["position"]["relativeTo"])
        self.assertEqual(SHAPE_W // 2, payload["position"]["x"])
        self.assertEqual(300, payload["position"]["y"])
        self.assertEqual("Поход к врачу", payload["data"]["title"])

    async def test_frame_payload_uses_supported_miro_color(self):
        service = RecordingMiroService()
        stats = {"created": 0, "updated": 0, "errors": 0}
        x, y = service._section_xy("tasks")

        await service._create_or_update_frame("", "ЗАДАЧИ", x, y, "#f6c000", 5007, stats)

        _, url, payload, _, _ = service.requests[0]
        self.assertTrue(url.endswith("/frames"))
        self.assertEqual("#f5d128", payload["style"]["fillColor"])
        self.assertEqual("custom", payload["data"]["format"])
        self.assertEqual("freeform", payload["data"]["type"])

    async def test_second_full_sync_updates_existing_mappings_without_duplicates(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        tz = ZoneInfo("Europe/Moscow")
        clock = FixedTimeService(tz, time(9), time(14), time(19), time(22))
        service = RecordingMiroService()

        async with sessions() as session:
            session.add_all(
                [
                    Task(user_id=1, title="Срочная задача", priority="high", description="Позвонить и подтвердить"),
                    Reminder(
                        user_id=1,
                        text="поход к врачу",
                        remind_at=datetime(2026, 7, 31, 18, 20, tzinfo=tz),
                    ),
                    ProblemBlock(
                        user_id=1,
                        title="Документы",
                        next_action="Собрать паспорт и полис",
                        priority="high",
                    ),
                    ScheduleOverride(user_id=1, date=clock.today(), mode="focus", description="Врач и документы"),
                ]
            )
            await session.commit()

        config = SimpleNamespace()
        async with sessions() as session:
            first = await service.sync_all(1, session, clock, config)
            await session.commit()
            first_mapping_count = await session.scalar(select(func.count()).select_from(MiroMapping))

        service.requests.clear()
        async with sessions() as session:
            second = await service.sync_all(1, session, clock, config)
            await session.commit()
            second_mapping_count = await session.scalar(select(func.count()).select_from(MiroMapping))

        self.assertEqual(first_mapping_count, second_mapping_count)
        self.assertGreater(first["created"], 0)
        self.assertEqual(0, second["created"])
        self.assertGreater(second["updated"], 0)
        self.assertEqual(1, second["tasks"])
        self.assertEqual(1, second["reminders"])
        self.assertEqual(1, second["problems"])
        self.assertTrue(any("/cards/" in url for _, url, *_ in service.requests))
        await engine.dispose()

    async def test_compact_sync_creates_only_five_operational_frames(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        tz = ZoneInfo("Europe/Moscow")
        clock = FixedTimeService(tz, time(9), time(14), time(19), time(22))
        service = RecordingMiroService()

        async with sessions() as session:
            session.add(Task(user_id=1, title="Собрать релиз", next_action="Открыть чеклист"))
            await session.commit()
        async with sessions() as session:
            await service.sync_all(1, session, clock, SimpleNamespace())
            await session.commit()

        frame_titles = [
            payload["data"]["title"]
            for method, url, payload, *_ in service.requests
            if method == "post" and url.endswith("/frames")
        ]
        self.assertEqual(5, len(frame_titles))
        self.assertEqual(
            {
                "ПЛАН НА СЕГОДНЯ",
                "СДЕЛАТЬ СЕЙЧАС",
                "ПОСЛЕ ЭТОГО",
                "ПРЕПЯТСТВИЯ",
                "ЗАВЕРШЕНО",
            },
            {title.split(" · ", 1)[0] for title in frame_titles},
        )
        self.assertFalse(
            any(url.endswith("/sticky_notes") for _, url, *_ in service.requests)
        )
        await engine.dispose()


if __name__ == "__main__":
    unittest.main()
