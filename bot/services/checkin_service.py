from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from bot.database.models import ActionLog
from bot.database.queries import (
    get_or_create_runtime_state,
    get_or_create_user_profile,
    touch_user_activity,
)
from bot.keyboards.inline import checkin_entity_keyboard
from bot.services.content_quality import is_meaningful_problem
from bot.services.datetime_utils import ensure_aware

logger = logging.getLogger(__name__)


class CheckinService:
    def __init__(
        self, scheduler, session_factory, time_service, problem_block_service,
        next_step_service, overload_service, allowed_user_ids: int | Iterable[int] | None = None,
        enabled: bool = False, morning_time=None, day_time=None, evening_time=None,
        *, allowed_user_id: int | None = None,
    ):
        self.scheduler = scheduler
        self.session_factory = session_factory
        self.time_service = time_service
        self.problem_block_service = problem_block_service
        self.next_step_service = next_step_service
        self.overload_service = overload_service
        # ``allowed_user_id`` remains as a keyword-only compatibility path for
        # older callers/tests while production uses the closed tuple allowlist.
        if allowed_user_ids is None:
            allowed_user_ids = allowed_user_id
        if allowed_user_ids is None:
            raise ValueError("At least one allowed user is required")
        if isinstance(allowed_user_ids, int):
            allowed_user_ids = (allowed_user_ids,)
        self.allowed_user_ids = tuple(dict.fromkeys(int(user_id) for user_id in allowed_user_ids))
        self.enabled = enabled
        self.morning_time = morning_time
        self.day_time = day_time
        self.evening_time = evening_time

    def schedule_daily_checkins(self, bot, timezone_by_user: dict[int, str] | None = None):
        for user_id in self.allowed_user_ids:
            timezone = (timezone_by_user or {}).get(user_id)
            self.reschedule_user_checkins(user_id, bot, timezone)

    def reschedule_user_checkins(self, user_id: int, bot, timezone: str | None = None):
        timezone = self._valid_timezone(timezone)
        for checkin_type, at in (
            ("morning", self.morning_time),
            ("day", self.day_time),
            ("evening", self.evening_time),
        ):
            self.scheduler.add_job(
                self.send_checkin,
                "cron",
                hour=at.hour,
                minute=at.minute,
                timezone=timezone,
                args=[user_id, checkin_type, bot],
                id=f"checkin:{checkin_type}:{user_id}",
                replace_existing=True,
            )

    async def should_send_checkin(self, user_id: int, checkin_type: str, session, now: datetime) -> bool:
        state = await get_or_create_runtime_state(
            session, user_id, default_checkin_enabled=self.enabled
        )
        tz = now.tzinfo
        if not state.checkin_enabled:
            logger.debug("checkin skip [%s %s]: disabled", user_id, checkin_type)
            return False
        quiet_until = ensure_aware(state.quiet_until, tz)
        if quiet_until and quiet_until > now:
            logger.debug("checkin skip [%s %s]: quiet mode until %s", user_id, checkin_type, quiet_until)
            return False
        last_activity = ensure_aware(state.last_user_activity_at, tz)
        if last_activity and last_activity >= now - timedelta(minutes=45):
            logger.debug("checkin skip [%s %s]: recent activity at %s", user_id, checkin_type, last_activity)
            return False
        if state.checkin_count_date != now.date():
            state.checkin_count_date = now.date()
            state.checkin_count_today = 0
        if state.checkin_count_today >= 1:
            logger.debug("checkin skip [%s %s]: daily limit reached", user_id, checkin_type)
            return False
        last_checkin = ensure_aware(state.last_checkin_at, tz)
        if last_checkin and last_checkin.date() == now.date() and last_checkin >= now - timedelta(hours=2):
            logger.debug("checkin skip [%s %s]: sent recently at %s", user_id, checkin_type, last_checkin)
            return False
        return True

    async def mark_checkin_sent(self, user_id: int, session, now: datetime):
        state = await get_or_create_runtime_state(session, user_id)
        if state.checkin_count_date != now.date():
            state.checkin_count_date = now.date()
            state.checkin_count_today = 0
        state.last_checkin_at = now
        state.checkin_count_today = (state.checkin_count_today or 0) + 1
        await session.flush()

    async def send_checkin(self, user_id: int, checkin_type: str, bot):
        async with self.session_factory() as session:
            profile = await get_or_create_user_profile(
                session, user_id, "", str(self.time_service.tz)
            )
            user_clock = self.time_service.in_timezone(
                self._valid_timezone(profile.timezone)
            )
            now = user_clock.now()
            ok = await self.should_send_checkin(user_id, checkin_type, session, now)
            if not ok:
                await session.commit()
                return
            block = await self.problem_block_service.pick_checkin_problem_block(user_id, session, checkin_type, now)
            payload = None
            if block and is_meaningful_problem(block):
                heading = (
                    f"Требует решения до {self._deadline_label(block.deadline, now)}:"
                    if block.deadline
                    else "Эта срочная проблема всё ещё открыта:"
                )
                payload = {
                    "text": (
                        f"{heading}\n{block.title}\n\nНачать с: {block.next_action}"
                    ),
                    "related_entities": [{"type": "problem_block", "id": block.id}],
                }
            if payload is None:
                payload = await self.next_step_service.build_proactive_suggestion(user_id, session, now)
            entities = (payload or {}).get("related_entities", [])
            entity = entities[0] if entities else None
            if not payload or not entity:
                logger.debug("checkin skip [%s %s]: no time-sensitive entity", user_id, checkin_type)
                await session.commit()
                return
            if await self._was_recently_sent(session, user_id, entity, now):
                logger.debug("checkin skip [%s %s]: entity sent recently", user_id, checkin_type)
                await session.commit()
                return
            await session.commit()

        await bot.send_message(
            user_id,
            escape(payload["text"]),
            reply_markup=checkin_entity_keyboard(entity),
        )
        async with self.session_factory() as session:
            await self.mark_checkin_sent(user_id, session, now)
            self._record_delivery(session, user_id, checkin_type, entity, payload["text"], now)
            await session.commit()

    async def _was_recently_sent(self, session, user_id: int, entity: dict, now: datetime) -> bool:
        result = await session.execute(
            select(ActionLog)
            .where(
                ActionLog.user_id == user_id,
                ActionLog.action_type == "proactive_hint",
                ActionLog.entity_type == entity["type"],
                ActionLog.entity_id == entity["id"],
            )
            .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        created_at = ensure_aware(latest.created_at, now.tzinfo) if latest else None
        return bool(created_at and created_at >= now - timedelta(hours=72))

    @staticmethod
    def _record_delivery(session, user_id: int, checkin_type: str, entity: dict, text: str, now: datetime):
        session.add(ActionLog(
            user_id=user_id,
            batch_key=f"hint:{entity['type']}:{entity['id']}:{now.date()}",
            action_type="proactive_hint",
            entity_type=entity["type"],
            entity_id=entity["id"],
            summary=text[:500],
            source=f"checkin_{checkin_type}",
            status="applied",
            undoable=False,
        ))

    @staticmethod
    def _deadline_label(deadline, now: datetime) -> str:
        value = ensure_aware(deadline, now.tzinfo)
        if value.date() == now.date():
            return f"сегодня {value.strftime('%H:%M')}"
        if value.date() == (now + timedelta(days=1)).date():
            return f"завтра {value.strftime('%H:%M')}"
        return value.strftime("%d.%m %H:%M")

    async def set_quiet_2h(self, user_id: int, session):
        state = await get_or_create_runtime_state(session, user_id)
        profile = await get_or_create_user_profile(
            session, user_id, "", str(self.time_service.tz)
        )
        state.quiet_until = self.time_service.in_timezone(
            self._valid_timezone(profile.timezone)
        ).now() + timedelta(hours=2)
        await session.flush()

    async def set_quiet_until_tomorrow(self, user_id: int, session):
        state = await get_or_create_runtime_state(session, user_id)
        profile = await get_or_create_user_profile(
            session, user_id, "", str(self.time_service.tz)
        )
        user_clock = self.time_service.in_timezone(
            self._valid_timezone(profile.timezone)
        )
        state.quiet_until = user_clock.build_datetime(
            "tomorrow", "morning"
        ).replace(hour=9, minute=0, second=0, microsecond=0)
        await session.flush()

    async def disable_checkins(self, user_id: int, session):
        state = await get_or_create_runtime_state(session, user_id)
        state.checkin_enabled = False
        await session.flush()

    async def touch_activity(self, user_id: int, session):
        profile = await get_or_create_user_profile(
            session, user_id, "", str(self.time_service.tz)
        )
        now = self.time_service.in_timezone(
            self._valid_timezone(profile.timezone)
        ).now()
        await touch_user_activity(session, user_id, now)

    def _valid_timezone(self, timezone: str | None):
        default_timezone = getattr(self.time_service, "tz", ZoneInfo("UTC"))
        try:
            return ZoneInfo(str(timezone or default_timezone))
        except ZoneInfoNotFoundError:
            return default_timezone
