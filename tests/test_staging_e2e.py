from __future__ import annotations

import unittest
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, PendingPreview, QualityFeedback, Task
from bot.handlers.domains import confirm_preview
from bot.handlers.operations import edit_preview_text, result_fix
from bot.handlers.quick_note import capture_text
from bot.services.action_log_service import ActionLogService
from bot.services.ai_service import AIService
from bot.services.conversation_repair_service import ConversationRepairService
from bot.services.intent_parser import IntentParser
from bot.services.metric_service import MetricService
from bot.services.project_service import ProjectService
from bot.services.quality_service import QualityService
from bot.services.task_prioritization_service import TaskPrioritizationService
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


class FakeMessage:
    def __init__(self, text: str, user_id: int = 1):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class FakeCallbackMessage:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data: str, user_id: int = 1):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCallbackMessage()

    async def answer(self):
        return None


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data.clear()
        self.state = None


class FakeScreen:
    async def delete_user_input(self, message):
        return None


class FakeScheduler:
    def schedule_reminder(self, reminder):
        return None

    def cancel_reminder_job(self, reminder_id):
        return None


class StagingE2ETests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        tz = ZoneInfo("Europe/Moscow")
        self.clock = TimeService(tz, time(9), time(14), time(19), time(22))
        self.clock.now = lambda: datetime(2026, 8, 1, 12, 0, tzinfo=tz)
        self.parser = IntentParser(NoAI(), self.clock)
        self.metrics = MetricService()
        self.quality = QualityService(self.metrics)
        self.logs = ActionLogService()
        self.repair = ConversationRepairService(self.clock)
        self.miro = SimpleNamespace(schedule=lambda user_id: None)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _confirm(self, preview_id: int):
        callback = FakeCallback(f"confirm_preview:{preview_id}")
        await confirm_preview(
            callback=callback,
            session_factory=self.sessions,
            reminder_scheduler=FakeScheduler(),
            time_service=self.clock,
            problem_block_service=SimpleNamespace(),
            problem_resources_service=SimpleNamespace(),
            action_log_service=self.logs,
            task_prioritization_service=TaskPrioritizationService(),
            project_service=ProjectService(),
            miro_sync_coordinator=self.miro,
            metric_service=self.metrics,
        )
        return callback

    async def test_text_to_preview_to_result_correction_rolls_back_whole_batch(self):
        message = FakeMessage("Купить лекарства")
        await capture_text(
            message=message,
            session_factory=self.sessions,
            intent_parser=self.parser,
            time_service=self.clock,
            screen_service=FakeScreen(),
            metric_service=self.metrics,
            assistant_ux_service=SimpleNamespace(),
            conversation_service=SimpleNamespace(QUERY_TYPES=set()),
            conversation_repair_service=self.repair,
        )
        async with self.sessions() as session:
            preview = await session.scalar(select(PendingPreview))
        self.assertIsNotNone(preview)
        self.assertIn("Добавить задачу", str(message.answers[0][1]))

        confirmed = await self._confirm(preview.id)
        self.assertIn("Всё верно", str(confirmed.message.edits[0][1]))
        state = FakeState()
        correction = FakeCallback(f"result_fix:{preview.id}")
        await result_fix(
            callback=correction,
            state=state,
            session_factory=self.sessions,
            action_log_service=self.logs,
            reminder_scheduler=FakeScheduler(),
            time_service=self.clock,
            miro_sync_coordinator=self.miro,
            metric_service=self.metrics,
            quality_service=self.quality,
        )
        self.assertIn("Сохранённое отменено", correction.message.edits[-1][0])

        corrected = FakeMessage("Купить витамины завтра на 15 минут")
        await edit_preview_text(
            message=corrected,
            state=state,
            session_factory=self.sessions,
            intent_parser=self.parser,
            screen_service=FakeScreen(),
            metric_service=self.metrics,
            quality_service=self.quality,
        )
        await self._confirm(preview.id)

        async with self.sessions() as session:
            tasks = list((await session.execute(select(Task))).scalars())
            feedback = list((await session.execute(select(QualityFeedback))).scalars())
        active = [task for task in tasks if task.status == "active"]
        self.assertEqual(["Купить витамины"], [task.title for task in active])
        self.assertEqual(1, len(tasks))
        self.assertEqual(
            {"correction_requested", "corrected"},
            {item.verdict for item in feedback},
        )


if __name__ == "__main__":
    unittest.main()
