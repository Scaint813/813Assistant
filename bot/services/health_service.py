from __future__ import annotations

import math
from datetime import date, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from bot.database.models import HealthNudge, HealthSnapshot
from bot.database.queries import get_or_create_runtime_state
from bot.services.datetime_utils import ensure_aware


class HealthService:
    def __init__(
        self,
        scheduler,
        session_factory,
        time_service,
        bot,
        user_id: int,
        enabled: bool,
        nudge_time,
        default_step_goal: int = 10000,
        min_step_gap: int = 1000,
    ):
        self.scheduler = scheduler
        self.session_factory = session_factory
        self.time_service = time_service
        self.bot = bot
        self.user_id = user_id
        self.enabled = enabled
        self.nudge_time = nudge_time
        self.default_step_goal = default_step_goal
        self.min_step_gap = min_step_gap

    def schedule_daily_nudge(self) -> None:
        if not self.enabled:
            return
        self.scheduler.add_job(
            self.send_step_nudge,
            "cron",
            hour=self.nudge_time.hour,
            minute=self.nudge_time.minute,
            id=f"health:steps:{self.user_id}",
            replace_existing=True,
        )

    async def upsert_snapshot(self, session, user_id: int, payload: dict) -> HealthSnapshot:
        snapshot_date = payload.get("date")
        if isinstance(snapshot_date, str):
            snapshot_date = date.fromisoformat(snapshot_date)
        snapshot_date = snapshot_date or self.time_service.today()
        result = await session.execute(
            select(HealthSnapshot).where(
                HealthSnapshot.user_id == user_id,
                HealthSnapshot.date == snapshot_date,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = HealthSnapshot(user_id=user_id, date=snapshot_date)
            session.add(row)
        row.steps = self._bounded_int(payload.get("steps"), 0, 200000)
        row.step_goal = self._bounded_int(
            payload.get("step_goal", self.default_step_goal), 1000, 100000
        )
        row.active_energy_kcal = self._bounded_int(payload.get("active_energy_kcal"), 0, 20000)
        row.workout_minutes = self._bounded_int(payload.get("workout_minutes"), 0, 1440)
        row.sleep_minutes = self._bounded_int(payload.get("sleep_minutes"), 0, 1440)
        row.resting_heart_rate = self._bounded_int(payload.get("resting_heart_rate"), 0, 250)
        row.hrv_ms = self._bounded_int(payload.get("hrv_ms"), 0, 500)
        row.source = str(payload.get("source") or "shortcut")[:32]
        row.captured_at = self.time_service.now()
        await session.flush()
        return row

    async def latest_snapshot(self, session, user_id: int) -> HealthSnapshot | None:
        result = await session.execute(
            select(HealthSnapshot)
            .where(HealthSnapshot.user_id == user_id)
            .order_by(HealthSnapshot.date.desc(), HealthSnapshot.captured_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def build_step_nudge(self, session, user_id: int, now: datetime) -> dict | None:
        snapshot = await self.latest_snapshot(session, user_id)
        if snapshot is None or snapshot.date != now.date():
            return None
        gap = max(0, snapshot.step_goal - snapshot.steps)
        if gap < self.min_step_gap or snapshot.workout_minutes >= 30:
            return None

        state = await get_or_create_runtime_state(session, user_id)
        if state.quiet_until and ensure_aware(state.quiet_until, now.tzinfo) > now:
            return None

        existing = await session.execute(
            select(HealthNudge).where(
                HealthNudge.user_id == user_id,
                HealthNudge.date == now.date(),
                HealthNudge.kind == "steps",
            )
        )
        if existing.scalar_one_or_none() is not None:
            return None

        minutes = max(10, int(math.ceil(gap / 105 / 5) * 5))
        hours_left = max(
            0,
            int((now.replace(hour=23, minute=0, second=0, microsecond=0) - now).total_seconds() // 3600),
        )
        return {
            "snapshot": snapshot,
            "gap": gap,
            "minutes": minutes,
            "text": (
                f"Сегодня {snapshot.steps:,} из {snapshot.step_goal:,} шагов. "
                f"Осталось {gap:,} — ориентировочно {minutes} минут спокойной ходьбы. "
                f"До 23:00 около {hours_left} ч.\n\n"
                "Запланировать прогулку?"
            ).replace(",", " "),
        }

    async def send_step_nudge(self) -> bool:
        if not self.enabled:
            return False
        now = self.time_service.now()
        async with self.session_factory() as session:
            payload = await self.build_step_nudge(session, self.user_id, now)
            if payload is None:
                return False
            snapshot = payload["snapshot"]
            snapshot_id = snapshot.id
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="Прогулка через 30 минут",
                    callback_data=f"health_walk_schedule:{snapshot_id}",
                )],
                [InlineKeyboardButton(
                    text="Напомнить через час",
                    callback_data=f"health_walk_later:{snapshot_id}",
                )],
                [InlineKeyboardButton(
                    text="Сегодня пропустить",
                    callback_data=f"health_walk_skip:{snapshot_id}",
                )],
            ]
        )
        # Записываем nudge только после успешной доставки. Иначе временная ошибка
        # Telegram навсегда подавила бы полезное напоминание на текущий день.
        await self.bot.send_message(self.user_id, payload["text"], reply_markup=keyboard)
        async with self.session_factory() as session:
            nudge = HealthNudge(
                user_id=self.user_id,
                date=now.date(),
                kind="steps",
                status="sent",
                snapshot_id=snapshot_id,
                sent_at=now,
            )
            session.add(nudge)
            await session.commit()
        return True

    async def mark_nudge(self, session, user_id: int, snapshot_id: int, status: str) -> None:
        result = await session.execute(
            select(HealthNudge).where(
                HealthNudge.user_id == user_id,
                HealthNudge.snapshot_id == snapshot_id,
                HealthNudge.kind == "steps",
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.status = status
            await session.flush()

    @staticmethod
    def _bounded_int(value, lower: int, upper: int) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            parsed = 0
        return max(lower, min(upper, parsed))
