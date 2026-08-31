from __future__ import annotations

import json
import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, Task, UserProfile
from bot.services.ai_service import AIService
from bot.services.intent_parser import IntentParser
from bot.services.time_service import TimeService


class FixedTimeService(TimeService):
    def now(self):
        return datetime(2026, 8, 1, 12, 0, tzinfo=self.tz)


class AIRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.service = AIService(
            "key",
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            reasoning_fast="low",
            reasoning_smart="medium",
            max_output_fast=1800,
            max_output_smart=5000,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_auto_routes_routine_to_luna_and_planning_to_terra(self):
        routine = self.service.request_policy(None, "Напомни в 18:20: врач", {})
        planning = self.service.request_policy(None, "Разбей запуск проекта на шаги", {})

        self.assertEqual("gpt-5.6-luna", routine.model)
        self.assertEqual("low", routine.reasoning_effort)
        self.assertEqual("gpt-5.6-terra", planning.model)
        self.assertEqual("medium", planning.reasoning_effort)

    async def test_user_modes_override_automatic_tier(self):
        economy = self.service.request_policy(
            None,
            "Составь глубокий план",
            {"assistant_preferences": {"ai_mode": "economy", "token_mode": "economy"}},
        )
        smart = self.service.request_policy(
            None,
            "Напомни позвонить",
            {"assistant_preferences": {"ai_mode": "smart", "token_mode": "maximum"}},
        )

        self.assertEqual(("fast", "gpt-5.6-luna", "none"), (economy.tier, economy.model, economy.reasoning_effort))
        self.assertLess(economy.max_completion_tokens, 1800)
        self.assertEqual(("smart", "gpt-5.6-terra", "high"), (smart.tier, smart.model, smart.reasoning_effort))
        self.assertGreater(smart.max_completion_tokens, 5000)

    async def test_reasoning_request_omits_temperature(self):
        policy = self.service.request_policy(None, "обычная задача", {})
        controls = self.service._request_controls(policy, temperature=0.2)

        self.assertEqual("low", controls["reasoning_effort"])
        self.assertNotIn("temperature", controls)
        self.assertEqual(1800, controls["max_completion_tokens"])

    async def test_saved_token_mode_changes_context_size(self):
        async with self.sessions() as session:
            session.add(
                UserProfile(
                    user_id=1,
                    preferences_json=json.dumps({"ai_mode": "auto", "token_mode": "economy"}),
                )
            )
            session.add_all([Task(user_id=1, title=f"Задача {index}") for index in range(8)])
            await session.commit()

        parser = IntentParser(
            self.service,
            FixedTimeService(ZoneInfo("Europe/Moscow"), time(9), time(14), time(19), time(22)),
        )
        context = await parser._build_context({"session_factory": self.sessions, "user_id": 1})

        self.assertEqual("economy", context["assistant_preferences"]["token_mode"])
        self.assertEqual(5, len(context["active_tasks"]))


if __name__ == "__main__":
    unittest.main()
