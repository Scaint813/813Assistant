from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, PendingPreview, QualityFeedback, ReminderDelivery
from bot.services.metric_service import MetricService
from bot.services.quality_service import QualityService


class QualityMetricTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_reports_parser_path_latency_voice_and_delivery_failure(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = MetricService()
        try:
            async with sessions() as session:
                await service.record(
                    session,
                    10,
                    "intent_parsed",
                    metadata={"parser_path": "openai", "source": "text", "latency_ms": 100},
                )
                await service.record(
                    session,
                    10,
                    "intent_parsed",
                    metadata={"parser_path": "fallback", "source": "voice", "latency_ms": 400},
                )
                session.add(
                    ReminderDelivery(
                        occurrence_key="delivery:1:quality-test",
                        reminder_id=1,
                        user_id=10,
                        scheduled_for=datetime.now(UTC),
                        status="failed",
                        attempts=3,
                    )
                )
                for index in range(5):
                    preview = PendingPreview(
                        user_id=10,
                        original_text=f"instruction {index}",
                        preview_json='{"intents": []}',
                    )
                    session.add(preview)
                    await session.flush()
                    session.add(
                        QualityFeedback(
                            user_id=10,
                            preview_id=preview.id,
                            stage="result",
                            verdict="corrected" if index < 2 else "accepted",
                        )
                    )
                for _ in range(5):
                    await service.record(session, 10, "clarification_requested")
                await service.record(session, 10, "clarification_resolved")
                session.add(
                    ReminderDelivery(
                        occurrence_key="delivery:2:quality-test",
                        reminder_id=2,
                        user_id=10,
                        scheduled_for=datetime.now(UTC) - timedelta(seconds=90),
                        delivered_at=datetime.now(UTC),
                        status="delivered",
                    )
                )
                await session.commit()

                summary = await service.summary(session, 10, datetime.now(UTC))
                evaluation = await QualityService(service).evaluate_user(
                    session, 10, datetime.now(UTC)
                )

            self.assertEqual(1, summary["parser_fallbacks"])
            self.assertEqual(1, summary["voice_requests"])
            self.assertEqual(400, summary["p95_latency_ms"])
            self.assertEqual(1, summary["reminder_deliveries_failed"])
            self.assertEqual(0.4, summary["preview_error_rate"])
            self.assertEqual(0.2, summary["clarification_resolution_rate"])
            self.assertEqual(90, summary["reminder_delivery_p95_seconds"])
            self.assertTrue(
                any("исправляется 40%" in item for item in evaluation["violations"])
            )
            self.assertTrue(
                any("только в 20%" in item for item in evaluation["violations"])
            )
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
