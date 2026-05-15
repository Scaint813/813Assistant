from __future__ import annotations

import logging
from datetime import datetime, timedelta

from bot.database.queries import get_or_create_runtime_state, get_upcoming_overrides, touch_user_activity
from bot.keyboards.inline import checkin_keyboard, overload_keyboard, quiet_keyboard

logger = logging.getLogger(__name__)


class CheckinService:
    def __init__(self, scheduler, session_factory, time_service, problem_block_service, next_step_service, overload_service, allowed_user_id: int, enabled: bool, morning_time, day_time, evening_time):
        self.scheduler = scheduler
        self.session_factory = session_factory
        self.time_service = time_service
        self.problem_block_service = problem_block_service
        self.next_step_service = next_step_service
        self.overload_service = overload_service
        self.allowed_user_id = allowed_user_id
        self.enabled = enabled
        self.morning_time = morning_time
        self.day_time = day_time
        self.evening_time = evening_time

    def schedule_daily_checkins(self, bot):
        if not self.enabled:
            logger.info("Check-ins disabled by config")
            return
        for checkin_type, at in (("morning", self.morning_time), ("day", self.day_time), ("evening", self.evening_time)):
            self.scheduler.add_job(
                self.send_checkin,
                "cron",
                hour=at.hour,
                minute=at.minute,
                args=[self.allowed_user_id, checkin_type, bot],
                id=f"checkin:{checkin_type}:{self.allowed_user_id}",
                replace_existing=True,
            )

    async def should_send_checkin(self, user_id: int, checkin_type: str, session, now: datetime) -> bool:
        state = await get_or_create_runtime_state(session, user_id)
        if not state.checkin_enabled:
            return False
        if state.quiet_until and state.quiet_until > now:
            return False
        if state.last_user_activity_at and state.last_user_activity_at >= now - timedelta(minutes=45):
            return False
        if state.last_checkin_at and state.last_checkin_at.date() == now.date() and state.last_checkin_at >= now - timedelta(hours=2):
            return False
        return True

    async def mark_checkin_sent(self, user_id: int, session, now: datetime):
        state = await get_or_create_runtime_state(session, user_id)
        state.last_checkin_at = now
        await session.flush()

    async def send_checkin(self, user_id: int, checkin_type: str, bot):
        now = self.time_service.now()
        async with self.session_factory() as session:
            ok = await self.should_send_checkin(user_id, checkin_type, session, now)
            if not ok:
                await session.commit()
                return
            block = await self.problem_block_service.pick_checkin_problem_block(user_id, session, checkin_type, now)
            overrides = await get_upcoming_overrides(session, user_id, now.date())
            rest_day = any(str(o.date) == str(now.date()) and o.mode == "rest_day" for o in overrides)
            await self.mark_checkin_sent(user_id, session, now)
            await session.commit()

        if block:
            if checkin_type == "morning":
                text = f"Штаб на связи.\n\nАктивный блок:\n{block.title}\n\nСледующий шаг:\n{block.next_action}"
            elif checkin_type == "day":
                text = f"Проверка.\n\nБлок ещё открыт:\n{block.title}\n\nСледующий шаг:\n{block.next_action}"
            else:
                text = f"Закрываем день.\n\nОткрытый блок:\n{block.title}\n\nЧто делаем?"
            await bot.send_message(user_id, text, reply_markup=checkin_keyboard())
            return

        if rest_day:
            await bot.send_message(user_id, "Штаб на связи. Сегодня мягкий режим: сон, еда, тело и 1 короткий шаг.", reply_markup=checkin_keyboard())
            return

        if checkin_type == "morning":
            text = "Штаб на связи.\n\nПроверка:\n1. Сон\n2. Еда\n3. Тело\n4. Главная задача"
        elif checkin_type == "day":
            text = "Проверка.\n\nВсё планово?\nСон / еда / тело / задачи"
        else:
            text = "Закрываем день.\n\nЧто фиксируем?\n1. Сделано\n2. Перенести\n3. Сон / тело\n4. Завтрашний фокус"
        await bot.send_message(user_id, text, reply_markup=checkin_keyboard())

    async def set_quiet_2h(self, user_id: int, session):
        state = await get_or_create_runtime_state(session, user_id)
        state.quiet_until = self.time_service.now() + timedelta(hours=2)
        await session.flush()

    async def set_quiet_until_tomorrow(self, user_id: int, session):
        state = await get_or_create_runtime_state(session, user_id)
        state.quiet_until = self.time_service.build_datetime("tomorrow", "morning").replace(hour=9, minute=0, second=0, microsecond=0)
        await session.flush()

    async def disable_checkins(self, user_id: int, session):
        state = await get_or_create_runtime_state(session, user_id)
        state.checkin_enabled = False
        await session.flush()

    async def touch_activity(self, user_id: int, session):
        await touch_user_activity(session, user_id, self.time_service.now())
