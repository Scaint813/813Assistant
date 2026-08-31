from __future__ import annotations

import json
import unittest
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.services.ai_service import AIService
from bot.services.intent_parser import IntentParser
from bot.services.time_service import TimeService


class FixedTimeService(TimeService):
    def now(self) -> datetime:
        return datetime(2026, 8, 1, 12, 0, tzinfo=self.tz)


class ConversationGoldenTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture = Path(__file__).parent / "fixtures" / "conversation_golden.json"
        cls.cases = json.loads(fixture.read_text(encoding="utf-8"))

    async def asyncSetUp(self) -> None:
        clock = FixedTimeService(
            ZoneInfo("Europe/Moscow"), time(9), time(14), time(19), time(22)
        )
        self.parser = IntentParser(AIService("", "", ""), clock)

    async def test_golden_conversations(self) -> None:
        failures = []
        for case in self.cases:
            if case.get("needs_data") or case.get("skip_parser"):
                continue
            with self.subTest(case=case["id"]):
                result = await self.parser.parse_user_text(case["text"], {})
                if case.get("clarification"):
                    if result.get("intents") or not result.get("clarification"):
                        failures.append(
                            f"{case['id']}: expected safe clarification, got {result}"
                        )
                    continue
                types = [item.get("type") for item in result.get("intents", [])]
                if types != case["types"]:
                    failures.append(f"{case['id']}: expected {case['types']}, got {types}")
                    continue
                first = result["intents"][0]
                expected = {
                    "title": case.get("task_title"),
                    "text": case.get("reminder_text"),
                    "remind_at": case.get("remind_at"),
                    "estimated_minutes": case.get("estimated_minutes"),
                }
                for field, value in expected.items():
                    if value is not None and first.get(field) != value:
                        failures.append(
                            f"{case['id']}: expected {field}={value!r}, "
                            f"got {first.get(field)!r}"
                        )
        self.assertEqual([], failures, "\n".join(failures))

    def test_fixture_has_meaningful_coverage(self) -> None:
        self.assertGreaterEqual(len(self.cases), 40)
        self.assertGreaterEqual(
            sum(bool(case.get("clarification")) for case in self.cases), 8
        )


if __name__ == "__main__":
    unittest.main()
