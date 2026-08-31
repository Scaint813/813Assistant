from __future__ import annotations

import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from bot.services.ai_service import AIService
from bot.services.intent_parser import IntentParser
from bot.services.time_service import TimeService


class FixedTimeService(TimeService):
    def now(self) -> datetime:
        return datetime(2026, 7, 31, 12, 0, tzinfo=self.tz)


class GenericReminderAI:
    async def parse_intent_with_openai(self, text, context):
        return {
            "intents": [
                {
                    "type": "create_reminder",
                    "text": "Напоминание",
                    "date": "today",
                    "time": "evening",
                }
            ]
        }

    def parse_intent_fallback(self, text):
        return {"intents": []}


def make_time_service() -> FixedTimeService:
    return FixedTimeService(
        ZoneInfo("Europe/Moscow"),
        time(9, 0),
        time(14, 0),
        time(19, 0),
        time(22, 0),
    )


class ReminderQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_preserves_subject_and_dot_time(self):
        parser = IntentParser(AIService("", "", ""), make_time_service())

        parsed = await parser.parse_user_text(
            "Время — 18.20, напоминание — поход к врачу", {}
        )

        reminder = parsed["intents"][0]
        self.assertEqual("create_reminder", reminder["type"])
        self.assertEqual("поход к врачу", reminder["text"])
        self.assertEqual("2026-07-31T18:20:00+03:00", reminder["remind_at"])

    async def test_generic_model_text_is_recovered_from_original(self):
        parser = IntentParser(GenericReminderAI(), make_time_service())

        parsed = await parser.parse_user_text(
            "Напоминание - поход к врачу, 18.20", {}
        )

        reminder = parsed["intents"][0]
        self.assertEqual("поход к врачу", reminder["text"])
        self.assertEqual("2026-07-31T18:20:00+03:00", reminder["remind_at"])

    async def test_explicit_tomorrow_and_colon_time(self):
        parser = IntentParser(AIService("", "", ""), make_time_service())

        parsed = await parser.parse_user_text(
            "Напомни завтра в 09:15 позвонить врачу", {}
        )

        reminder = parsed["intents"][0]
        self.assertEqual("позвонить врачу", reminder["text"])
        self.assertEqual("2026-08-01T09:15:00+03:00", reminder["remind_at"])

    async def test_bare_past_time_uses_next_occurrence(self):
        parser = IntentParser(AIService("", "", ""), make_time_service())

        parsed = await parser.parse_user_text("Напомни в 09:00 позвонить врачу", {})

        self.assertEqual(
            "2026-08-01T09:00:00+03:00",
            parsed["intents"][0]["remind_at"],
        )

    async def test_missing_subject_requests_clarification(self):
        parser = IntentParser(AIService("", "", ""), make_time_service())

        parsed = await parser.parse_user_text("Напоминание в 18.20", {})

        self.assertEqual([], parsed["intents"])
        self.assertIn("О чём напомнить", parsed["clarification"])


if __name__ == "__main__":
    unittest.main()
