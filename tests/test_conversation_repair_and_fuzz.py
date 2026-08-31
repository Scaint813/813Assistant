from __future__ import annotations

import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, ConversationRepair
from bot.services.ai_service import AIService
from bot.services.conversation_repair_service import ConversationRepairService
from bot.services.intent_parser import IntentParser
from bot.services.time_service import TimeService


class NoAI:
    def __init__(self):
        self.last_usage = {}

    async def parse_intent_with_openai(self, text, context):
        return None

    def parse_intent_fallback(self, text):
        return AIService.parse_intent_fallback(self, text)

    def parse_intent_fallback_batch(self, text):
        return AIService.parse_intent_fallback_batch(self, text)

    _clean_target = staticmethod(AIService._clean_target)
    _clean_project_request = staticmethod(AIService._clean_project_request)


class TruncatedSourceAI(NoAI):
    """Mimic a live model that omits the follow-up from source_text."""

    async def parse_intent_with_openai(self, text, context):
        intent = {
            "type": "create_reminder",
            "text": "купить билеты домой",
            "source_text": "Напомни про купить билеты домой",
        }
        if "18:20" in text:
            intent.update({"date": "today", "time": "18:20"})
        return {"intents": [intent]}


class ConversationRepairAndFuzzTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tz = ZoneInfo("Europe/Moscow")
        self.clock = TimeService(self.tz, time(9), time(14), time(19), time(22))
        self.clock.now = lambda: datetime(2026, 8, 1, 12, 0, tzinfo=self.tz)
        self.parser = IntentParser(NoAI(), self.clock)
        self.repair = ConversationRepairService(self.clock)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_missing_time_is_resolved_by_one_short_followup(self):
        parsed = await self.parser.parse_user_text(
            "Напомни про поход к врачу", {"session_factory": self.sessions, "user_id": 1}
        )
        self.assertEqual("time", parsed["repair"]["missing_field"])
        async with self.sessions() as session:
            await self.repair.remember(session, 1, parsed["repair"], self.clock.now())
            await session.commit()
        async with self.sessions() as session:
            merged, resolved = await self.repair.merge_followup(
                session, 1, "сегодня в 18:20", self.clock.now()
            )
            await session.commit()
        final = await self.parser.parse_user_text(
            merged, {"session_factory": self.sessions, "user_id": 1}
        )
        self.assertTrue(resolved)
        self.assertEqual("поход к врачу", final["intents"][0]["text"])
        self.assertIn("T18:20", final["intents"][0]["remind_at"])

    async def test_model_cannot_drop_time_from_followup_source_text(self):
        parser = IntentParser(TruncatedSourceAI(), self.clock)
        parsed = await parser.parse_user_text(
            "Напомни про купить билеты домой",
            {"session_factory": self.sessions, "user_id": 1},
        )
        self.assertEqual("time", parsed["repair"]["missing_field"])

        async with self.sessions() as session:
            await self.repair.remember(session, 1, parsed["repair"], self.clock.now())
            await session.commit()
        async with self.sessions() as session:
            merged, resolved = await self.repair.merge_followup(
                session, 1, "сегодня в 18:20", self.clock.now()
            )
            await session.commit()

        final = await parser.parse_user_text(
            merged, {"session_factory": self.sessions, "user_id": 1}
        )
        self.assertTrue(resolved)
        self.assertFalse(final.get("clarification"))
        self.assertEqual("купить билеты домой", final["intents"][0]["text"])
        self.assertEqual(
            "2026-08-01T18:20:00+03:00",
            final["intents"][0]["remind_at"],
        )

    async def test_pending_repairs_never_cross_users(self):
        async with self.sessions() as session:
            await self.repair.remember(
                session,
                1,
                {
                    "kind": "create_reminder",
                    "missing_field": "time",
                    "original_text": "Напомни про врача",
                },
                self.clock.now(),
            )
            merged, resolved = await self.repair.merge_followup(
                session, 2, "сегодня в 18:20", self.clock.now()
            )
            row = await session.scalar(
                select(ConversationRepair).where(ConversationRepair.user_id == 1)
            )
        self.assertFalse(resolved)
        self.assertEqual("сегодня в 18:20", merged)
        self.assertIsNotNone(row)

    async def test_new_command_replaces_pending_repair_instead_of_being_hijacked(self):
        async with self.sessions() as session:
            await self.repair.remember(
                session,
                1,
                {
                    "kind": "create_reminder",
                    "missing_field": "time",
                    "original_text": "Напомни про врача",
                },
                self.clock.now(),
            )
            merged, resolved = await self.repair.merge_followup(
                session, 1, "добавь встречу завтра в 18:20", self.clock.now()
            )
            row = await session.scalar(
                select(ConversationRepair).where(ConversationRepair.user_id == 1)
            )
        self.assertFalse(resolved)
        self.assertEqual("добавь встречу завтра в 18:20", merged)
        self.assertIsNone(row)

    async def test_time_and_feedback_fuzz_corpus(self):
        cases = {
            "Напомни сегодня в 18.20 про врача": "18:20",
            "Напомни завтра к 7 про лекарство": "07:00",
            "Напомни вечером позвонить брату": "19:00",
            "Напомни через 45 минут сделать перерыв": "12:45",
        }
        for text, expected_time in cases.items():
            with self.subTest(text=text):
                parsed = await self.parser.parse_user_text(
                    text, {"session_factory": self.sessions, "user_id": 1}
                )
                self.assertFalse(parsed.get("clarification"))
                self.assertIn(expected_time, parsed["intents"][0]["remind_at"])
        for text in ("что за бред, бот?", "ты понял неправильно", "зачем это сообщение?"):
            with self.subTest(feedback=text):
                parsed = await self.parser.parse_user_text(
                    text, {"session_factory": self.sessions, "user_id": 1}
                )
                self.assertEqual([], parsed["intents"])


if __name__ == "__main__":
    unittest.main()
