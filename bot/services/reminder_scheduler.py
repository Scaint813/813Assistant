from __future__ import annotations

import asyncio
import logging
from calendar import monthrange
from collections.abc import Iterable
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.database.models import ActionLog, Reminder, ReminderDelivery
from bot.database.queries import get_all_active_reminders
from bot.keyboards.inline import reminder_keyboard
from bot.services.datetime_utils import ensure_aware

logger = logging.getLogger(__name__)
RECOVERY_WINDOW = timedelta(hours=2)


class ReminderScheduler:
    def __init__(self, tz, bot, session_factory, allowed_user_ids: Iterable[int] | None = None):
        self.scheduler = AsyncIOScheduler(timezone=tz)
        self.bot = bot
        self.session_factory = session_factory
        self.tz = tz
        self.allowed_user_ids = (
            frozenset(int(user_id) for user_id in allowed_user_ids)
            if allowed_user_ids is not None
            else None
        )
        self._delivery_locks: dict[int, asyncio.Lock] = {}

    def _is_allowed(self, user_id: int) -> bool:
        return self.allowed_user_ids is None or user_id in self.allowed_user_ids

    async def start_scheduler(self):
        if not self.scheduler.running:
            self.scheduler.start()
        async with self.session_factory() as session:
            reminders = await get_all_active_reminders(session)
        for reminder in reminders:
            if self._is_allowed(reminder.user_id):
                self.schedule_reminder(reminder, recover_overdue=True)

    def shutdown_scheduler(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _timezone_for(self, reminder: Reminder):
        try:
            return ZoneInfo(reminder.timezone or str(self.tz))
        except ZoneInfoNotFoundError:
            return self.tz

    def _normalize_dt(self, dt: datetime, tz=None) -> datetime:
        tz = tz or self.tz
        return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)

    def schedule_reminder(self, reminder: Reminder, *, recover_overdue: bool = False):
        if not self._is_allowed(reminder.user_id):
            logger.warning("Reminder %s belongs to a user outside the allowlist", reminder.id)
            return
        reminder_tz = self._timezone_for(reminder)
        remind_at = self._normalize_dt(reminder.remind_at, reminder_tz)
        now = datetime.now(tz=reminder_tz)
        if remind_at <= now:
            delivered_at = ensure_aware(reminder.last_delivered_at, self.tz)
            already_delivered = bool(delivered_at and delivered_at >= remind_at)
            recent_enough = remind_at >= now - RECOVERY_WINDOW
            if not recover_overdue or already_delivered or not recent_enough:
                return
            remind_at = now + timedelta(seconds=1)
            logger.warning("Recovering overdue reminder %s after restart", reminder.id)
        if not self.scheduler.running:
            logger.error("Scheduler not running; reminder %s saved but not scheduled", reminder.id)
            return
        self.scheduler.add_job(
            self.send_reminder,
            "date",
            run_date=remind_at,
            args=[reminder.id],
            id=f"reminder:{reminder.id}",
            replace_existing=True,
        )

    async def reschedule_reminder(self, reminder_id: int, new_remind_at: datetime):
        new_remind_at = self._normalize_dt(new_remind_at, new_remind_at.tzinfo or self.tz)
        async with self.session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            if not reminder:
                logger.warning("Reminder not found for reschedule: %s", reminder_id)
                self.cancel_reminder_job(reminder_id)
                return None
            if not self._is_allowed(reminder.user_id):
                logger.warning("Refusing to reschedule reminder %s outside the allowlist", reminder_id)
                self.cancel_reminder_job(reminder_id)
                return None
            reminder.remind_at = new_remind_at
            reminder.timezone = getattr(new_remind_at.tzinfo, "key", None) or reminder.timezone
            reminder.status = "active"
            await session.commit()
            await session.refresh(reminder)
        self.cancel_reminder_job(reminder_id)
        self.schedule_reminder(reminder)
        return reminder

    def cancel_reminder_job(self, reminder_id: int):
        job = self.scheduler.get_job(f"reminder:{reminder_id}")
        if job:
            job.remove()

    async def send_reminder(self, reminder_id: int):
        lock = self._delivery_locks.setdefault(reminder_id, asyncio.Lock())
        async with lock:
            await self._send_reminder_locked(reminder_id)

    async def _send_reminder_locked(self, reminder_id: int):
        occurrence_key = ""
        telegram_accepted = False
        telegram_message_id = None
        try:
            async with self.session_factory() as session:
                reminder = await session.get(Reminder, reminder_id)
                if not reminder:
                    logger.warning("Reminder not found for job reminder:%s", reminder_id)
                    self.cancel_reminder_job(reminder_id)
                    return
                if not self._is_allowed(reminder.user_id):
                    logger.warning("Skipping reminder %s outside the allowlist", reminder_id)
                    self.cancel_reminder_job(reminder_id)
                    return
                if reminder.status != "active":
                    return
                reminder_tz = self._timezone_for(reminder)
                remind_at = self._normalize_dt(reminder.remind_at, reminder_tz)
                occurrence_key = self._occurrence_key(reminder.id, remind_at)
                text = (
                    f"⏰ <b>{escape(reminder.text)}</b>\n\n"
                    f"Запланировано: {remind_at.strftime('%d.%m.%Y в %H:%M')}\n"
                    f"Часовой пояс: {reminder.timezone}"
                )
                if reminder.recurrence and reminder.recurrence != "none":
                    text += f"\nПовтор: {self._recurrence_label(reminder.recurrence)}"
            claimed = await self._claim_delivery(reminder, occurrence_key, remind_at)
            if not claimed:
                logger.info("Skipping duplicate delivery occurrence %s", occurrence_key)
                return

            message = await self.bot.send_message(
                reminder.user_id,
                text,
                reply_markup=reminder_keyboard(reminder.id),
            )
            telegram_accepted = True
            telegram_message_id = getattr(message, "message_id", None)
            async with self.session_factory() as session:
                reminder = await session.get(Reminder, reminder_id)
                delivery_result = await session.execute(
                    select(ReminderDelivery).where(
                        ReminderDelivery.occurrence_key == occurrence_key
                    )
                )
                delivery = delivery_result.scalar_one_or_none()
                if delivery:
                    delivery.status = "delivered"
                    delivery.telegram_message_id = telegram_message_id
                    delivery.delivered_at = datetime.now(tz=self.tz)
                    delivery.last_error = ""
                if not reminder:
                    await session.commit()
                    return
                reminder_tz = self._timezone_for(reminder)
                now = datetime.now(tz=reminder_tz)
                reminder.last_delivered_at = now
                reminder.delivery_attempts = 0
                reminder.last_delivery_error = ""
                session.add(ActionLog(
                    user_id=reminder.user_id,
                    batch_key=occurrence_key,
                    action_type="deliver",
                    entity_type="reminder",
                    entity_id=reminder.id,
                    summary=f"Доставлено напоминание: {reminder.text}",
                    source="scheduler",
                    status="applied",
                    undoable=False,
                ))
                next_at = self._next_recurrence(
                    self._normalize_dt(reminder.remind_at, reminder_tz),
                    reminder.recurrence,
                    now,
                )
                if next_at:
                    reminder.remind_at = next_at
                await session.commit()
                await session.refresh(reminder)
            if next_at:
                self.schedule_reminder(reminder)
        except Exception as exc:
            logger.exception("Failed to send reminder %s", reminder_id)
            attempts = 0
            async with self.session_factory() as session:
                reminder = await session.get(Reminder, reminder_id)
                if reminder:
                    if not telegram_accepted:
                        reminder.delivery_attempts = int(reminder.delivery_attempts or 0) + 1
                    reminder.last_delivery_error = str(exc)[:500]
                    attempts = reminder.delivery_attempts
                if occurrence_key:
                    delivery_result = await session.execute(
                        select(ReminderDelivery).where(
                            ReminderDelivery.occurrence_key == occurrence_key
                        )
                    )
                    delivery = delivery_result.scalar_one_or_none()
                    if delivery:
                        delivery.status = "uncertain" if telegram_accepted else "failed"
                        delivery.telegram_message_id = telegram_message_id
                        delivery.last_error = (
                            "Telegram accepted the message; persistence failed: "
                            if telegram_accepted
                            else ""
                        ) + str(exc)[:400]
                await session.commit()
            if not telegram_accepted and 0 < attempts <= 3 and self.scheduler.running:
                retry_at = datetime.now(tz=self.tz) + timedelta(minutes=5 * attempts)
                self.scheduler.add_job(
                    self.send_reminder,
                    "date",
                    run_date=retry_at,
                    args=[reminder_id],
                    id=f"reminder:{reminder_id}",
                    replace_existing=True,
                )

    async def _claim_delivery(
        self,
        reminder: Reminder,
        occurrence_key: str,
        scheduled_for: datetime,
    ) -> bool:
        now = datetime.now(tz=self.tz)
        async with self.session_factory() as session:
            result = await session.execute(
                select(ReminderDelivery).where(
                    ReminderDelivery.occurrence_key == occurrence_key
                )
            )
            delivery = result.scalar_one_or_none()
            if delivery:
                if delivery.status in {"delivered", "uncertain"}:
                    return False
                updated_at = ensure_aware(delivery.updated_at, self.tz)
                if delivery.status == "pending":
                    if updated_at and updated_at >= now - timedelta(minutes=10):
                        return False
                    # The process may have died after Telegram accepted the message but
                    # before the success commit. Retrying could create a visible duplicate.
                    delivery.status = "uncertain"
                    delivery.last_error = "stale pending delivery; manual review required"
                    await session.commit()
                    return False
                delivery.status = "pending"
                delivery.attempts = int(delivery.attempts or 0) + 1
                delivery.last_error = ""
            else:
                session.add(ReminderDelivery(
                    reminder_id=reminder.id,
                    user_id=reminder.user_id,
                    occurrence_key=occurrence_key,
                    scheduled_for=scheduled_for,
                    status="pending",
                ))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
        return True

    @staticmethod
    def _occurrence_key(reminder_id: int, remind_at: datetime) -> str:
        return f"delivery:{reminder_id}:{int(remind_at.timestamp())}"

    @staticmethod
    def _recurrence_label(recurrence: str) -> str:
        return {
            "daily": "каждый день",
            "weekdays": "по будням",
            "weekly": "раз в неделю",
            "monthly": "раз в месяц",
        }.get(recurrence, recurrence)

    @staticmethod
    def _next_recurrence(current: datetime, recurrence: str, after: datetime) -> datetime | None:
        if recurrence in {"", "none", None}:
            return None
        candidate = current
        if recurrence == "daily":
            while candidate <= after:
                candidate += timedelta(days=1)
        elif recurrence == "weekdays":
            while candidate <= after:
                candidate += timedelta(days=1)
                while candidate.weekday() >= 5:
                    candidate += timedelta(days=1)
        elif recurrence == "weekly":
            while candidate <= after:
                candidate += timedelta(days=7)
        elif recurrence == "monthly":
            while candidate <= after:
                year = candidate.year + (candidate.month == 12)
                month = 1 if candidate.month == 12 else candidate.month + 1
                candidate = candidate.replace(
                    year=year,
                    month=month,
                    day=min(candidate.day, monthrange(year, month)[1]),
                )
        else:
            return None
        return candidate
