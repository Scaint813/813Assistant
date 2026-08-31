from __future__ import annotations

import hashlib
import json
from datetime import datetime

from bot.database.models import PendingPreview, QualityFeedback


class QualityService:
    """Collect explicit user labels and evaluate measurable product SLOs."""

    MAX_PREVIEW_ERROR_RATE = 0.20
    MIN_CLARIFICATION_RESOLUTION_RATE = 0.80
    MAX_PARSE_P95_MS = 8_000
    MAX_REMINDER_DELIVERY_P95_SECONDS = 60
    MIN_SAMPLE_SIZE = 5

    def __init__(self, metric_service=None):
        self.metric_service = metric_service

    async def record_feedback(
        self,
        session,
        user_id: int,
        preview: PendingPreview | None,
        *,
        stage: str,
        verdict: str,
        corrected_text: str = "",
        metadata: dict | None = None,
    ) -> QualityFeedback:
        original = preview.original_text if preview else ""
        row = QualityFeedback(
            user_id=user_id,
            preview_id=preview.id if preview else None,
            stage=stage[:16],
            verdict=verdict[:32],
            original_hash=self._hash(original),
            corrected_hash=self._hash(corrected_text),
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        )
        session.add(row)
        await session.flush()
        return row

    async def evaluate_user(self, session, user_id: int, now: datetime, days: int = 7) -> dict:
        if self.metric_service is None:
            raise RuntimeError("MetricService is required for SLO evaluation")
        summary = await self.metric_service.summary(session, user_id, now, days)
        violations: list[str] = []
        samples = int(summary.get("previews_reviewed") or 0)
        if samples >= self.MIN_SAMPLE_SIZE and summary["preview_error_rate"] > self.MAX_PREVIEW_ERROR_RATE:
            violations.append(
                f"исправляется {summary['preview_error_rate']:.0%} результатов (цель ≤20%)"
            )
        clarification_requests = int(summary.get("clarification_requests") or 0)
        if (
            clarification_requests >= self.MIN_SAMPLE_SIZE
            and summary["clarification_resolution_rate"] < self.MIN_CLARIFICATION_RESOLUTION_RATE
        ):
            violations.append(
                "одношаговые уточнения завершаются только в "
                f"{summary['clarification_resolution_rate']:.0%} случаев (цель ≥80%)"
            )
        parse_samples = int(summary.get("parse_samples") or 0)
        if parse_samples >= self.MIN_SAMPLE_SIZE and summary["p95_latency_ms"] > self.MAX_PARSE_P95_MS:
            violations.append(
                f"p95 разбора {summary['p95_latency_ms']} мс (цель ≤8000 мс)"
            )
        if summary["reminder_deliveries_failed"]:
            violations.append(
                f"неудачных доставок напоминаний: {summary['reminder_deliveries_failed']}"
            )
        delivery_samples = int(summary.get("reminder_delivery_samples") or 0)
        if (
            delivery_samples >= 3
            and summary["reminder_delivery_p95_seconds"] > self.MAX_REMINDER_DELIVERY_P95_SECONDS
        ):
            violations.append(
                "p95 задержки напоминаний "
                f"{summary['reminder_delivery_p95_seconds']} сек. (цель ≤60 сек.)"
            )
        return {"summary": summary, "violations": violations}

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.strip().encode("utf-8")).hexdigest() if value.strip() else ""
