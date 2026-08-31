from __future__ import annotations

import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from bot.services.ai_service import AIService
from bot.services.intent_parser import IntentParser
from bot.services.time_service import TimeService


class FixedTimeService(TimeService):
    def now(self) -> datetime:
        return datetime(2026, 8, 1, 10, 0, tzinfo=self.tz)


class GenericTaskAI:
    async def parse_intent_with_openai(self, text, context):
        return {"intents": [{"type": "create_task", "title": "Задача"}]}

    def parse_intent_fallback(self, text):
        return {"intents": []}


def make_time_service() -> FixedTimeService:
    return FixedTimeService(
        ZoneInfo("Europe/Moscow"),
        time(9),
        time(14),
        time(19),
        time(22),
    )


class TaskQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_model_title_is_recovered_from_original_request(self):
        parser = IntentParser(GenericTaskAI(), make_time_service())

        parsed = await parser.parse_user_text(
            "Добавь задачу: подготовить документы к врачу", {}
        )

        task = parsed["intents"][0]
        self.assertEqual("подготовить документы к врачу", task["title"])
        self.assertEqual("подготовить документы к врачу", task["next_action"])

    async def test_only_word_task_requests_clarification(self):
        parser = IntentParser(AIService("", "", ""), make_time_service())

        parsed = await parser.parse_user_text("Задача", {})

        self.assertEqual([], parsed["intents"])
        self.assertIn("Что именно нужно сделать", parsed["clarification"])


if __name__ == "__main__":
    unittest.main()
