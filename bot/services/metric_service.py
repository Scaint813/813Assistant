from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import func, select

from bot.database.models import ActionLog, ProductMetric, QualityFeedback, ReminderDelivery
from bot.services.datetime_utils import ensure_aware


class MetricService:
    async def record(
        self, session, user_id: int, event_type: str, *, entity_type: str = "",
        entity_id: int | None = None, usage: dict | None = None, metadata: dict | None = None,
    ) -> ProductMetric:
        usage = usage or {}
        row = ProductMetric(
            user_id=user_id,
            event_type=event_type[:64],
            entity_type=entity_type[:64],
            entity_id=entity_id,
            model=str(usage.get("model") or "")[:128],
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        )
        session.add(row)
        await session.flush()
        return row

    async def summary(self, session, user_id: int, now: datetime, days: int = 30) -> dict:
        since = now - timedelta(days=days)
        result = await session.execute(
            select(
                ProductMetric.event_type,
                func.count(ProductMetric.id),
                func.sum(ProductMetric.input_tokens),
                func.sum(ProductMetric.output_tokens),
            ).where(
                ProductMetric.user_id == user_id,
                ProductMetric.created_at >= since,
            ).group_by(ProductMetric.event_type)
        )
        events = {}
        total_input = total_output = 0
        for event_type, count, input_tokens, output_tokens in result.all():
            events[event_type] = count
            total_input += int(input_tokens or 0)
            total_output += int(output_tokens or 0)
        parser_result = await session.execute(
            select(ProductMetric.metadata_json).where(
                ProductMetric.user_id == user_id,
                ProductMetric.event_type == "intent_parsed",
                ProductMetric.created_at >= since,
            )
        )
        parser_fallbacks = voice_requests = 0
        latencies = []
        for (metadata_json,) in parser_result.all():
            try:
                metadata = json.loads(metadata_json or "{}")
            except (TypeError, ValueError):
                metadata = {}
            parser_fallbacks += int(metadata.get("parser_path") == "fallback")
            voice_requests += int(metadata.get("source") == "voice")
            if isinstance(metadata.get("latency_ms"), int | float):
                latencies.append(max(0, int(metadata["latency_ms"])))
        latencies.sort()
        p95_latency_ms = latencies[round((len(latencies) - 1) * 0.95)] if latencies else 0
        delivery_result = await session.execute(
            select(func.count(ActionLog.id)).where(
                ActionLog.user_id == user_id,
                ActionLog.action_type == "deliver",
                ActionLog.entity_type == "reminder",
                ActionLog.created_at >= since,
            )
        )
        reminder_actions_result = await session.execute(
            select(func.count(ActionLog.id)).where(
                ActionLog.user_id == user_id,
                ActionLog.action_type == "update",
                ActionLog.entity_type == "reminder",
                ActionLog.created_at >= since,
            )
        )
        failed_delivery_result = await session.execute(
            select(func.count(ReminderDelivery.id)).where(
                ReminderDelivery.user_id == user_id,
                ReminderDelivery.status == "failed",
                ReminderDelivery.created_at >= since,
            )
        )
        feedback_result = await session.execute(
            select(QualityFeedback.preview_id, QualityFeedback.verdict).where(
                QualityFeedback.user_id == user_id,
                QualityFeedback.created_at >= since,
            )
        )
        reviewed_preview_ids: set[int] = set()
        error_preview_ids: set[int] = set()
        for preview_id, verdict in feedback_result.all():
            if preview_id is None:
                continue
            reviewed_preview_ids.add(preview_id)
            if verdict in {"corrected", "not_action", "correction_requested"}:
                error_preview_ids.add(preview_id)
        delivery_lag_result = await session.execute(
            select(ReminderDelivery.scheduled_for, ReminderDelivery.delivered_at).where(
                ReminderDelivery.user_id == user_id,
                ReminderDelivery.status == "delivered",
                ReminderDelivery.delivered_at.is_not(None),
                ReminderDelivery.created_at >= since,
            )
        )
        delivery_lags = []
        for scheduled_for, delivered_at in delivery_lag_result.all():
            delivered = ensure_aware(delivered_at, now.tzinfo)
            scheduled = ensure_aware(scheduled_for, now.tzinfo)
            if delivered and scheduled:
                delivery_lags.append(max(0, round((delivered - scheduled).total_seconds())))
        delivery_lags.sort()
        delivery_p95 = (
            delivery_lags[round((len(delivery_lags) - 1) * 0.95)]
            if delivery_lags
            else 0
        )
        reviewed_count = len(reviewed_preview_ids)
        clarification_requests = int(events.get("clarification_requested") or 0)
        clarification_resolved = int(events.get("clarification_resolved") or 0)
        return {
            "events": events,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "days": days,
            "reminders_delivered": int(delivery_result.scalar_one() or 0),
            "reminder_actions": int(reminder_actions_result.scalar_one() or 0),
            "reminder_deliveries_failed": int(failed_delivery_result.scalar_one() or 0),
            "parser_fallbacks": parser_fallbacks,
            "voice_requests": voice_requests,
            "p95_latency_ms": p95_latency_ms,
            "parse_samples": len(latencies),
            "previews_reviewed": reviewed_count,
            "preview_errors": len(error_preview_ids),
            "preview_error_rate": (
                len(error_preview_ids) / reviewed_count if reviewed_count else 0.0
            ),
            "clarification_requests": clarification_requests,
            "clarification_resolved": clarification_resolved,
            "clarification_resolution_rate": (
                min(1.0, clarification_resolved / clarification_requests)
                if clarification_requests
                else 1.0
            ),
            "reminder_delivery_samples": len(delivery_lags),
            "reminder_delivery_p95_seconds": delivery_p95,
        }
