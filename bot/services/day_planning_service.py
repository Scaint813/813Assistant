from __future__ import annotations

from datetime import datetime, time, timedelta
from html import escape

from sqlalchemy import delete, select

from bot.database.models import ScheduleOverride, TaskPlanBlock
from bot.services.datetime_utils import ensure_aware


class DayPlanningService:
    """Build and apply an explicit, reversible-looking overflow proposal."""

    def __init__(self, calendar_service, horizon_days: int = 30):
        self.calendar_service = calendar_service
        self.horizon_days = horizon_days

    async def build_overflow_proposal(self, session, user_id: int, now: datetime) -> dict:
        today = await self.calendar_service.build_day_plan(
            session, user_id, now, day=now.date()
        )
        planning = today["planning"]
        remaining: dict[int, dict] = {}

        for box in today["timeboxes"]:
            minutes = int(box.get("remaining_minutes") or 0)
            if minutes:
                remaining[box["task"].id] = {
                    "task": box["task"],
                    "minutes": minutes,
                }
        for task in today["unscheduled"]:
            remaining[task.id] = {
                "task": task,
                "minutes": max(5, int(task.estimated_minutes or 30)),
            }

        candidates = list(remaining.values())
        candidates.sort(key=lambda item: (
            ensure_aware(item["task"].deadline, now.tzinfo)
            if item["task"].deadline else now + timedelta(days=3650),
            item["task"].id,
        ))
        proposal = []
        skipped_rest_days = []
        override_result = await session.execute(
            select(ScheduleOverride).where(
                ScheduleOverride.user_id == user_id,
                ScheduleOverride.date > now.date(),
                ScheduleOverride.date <= now.date() + timedelta(days=self.horizon_days),
            )
        )
        overrides = {item.date: item for item in override_result.scalars().all()}
        for offset in range(1, self.horizon_days + 1):
            if not any(item["minutes"] > 0 for item in candidates):
                break
            day = now.date() + timedelta(days=offset)
            override = overrides.get(day)
            is_weekend = day.weekday() >= 5
            if (
                (is_weekend and not planning["planning_weekends"])
                or (override and (override.mode == "rest_day" or not override.create_tasks))
            ):
                skipped_rest_days.append(day)
                continue
            window_start = datetime.combine(
                day, time(planning["workday_start_hour"]), tzinfo=now.tzinfo
            )
            window_end = datetime.combine(
                day, time(planning["workday_end_hour"]), tzinfo=now.tzinfo
            )
            events = await self.calendar_service.events_between(
                session, user_id, window_start, window_end
            )
            workouts = await self.calendar_service.workouts_between(
                session, user_id, window_start, window_end, now.tzinfo
            )
            free = self.calendar_service._free_intervals(
                window_start, window_end, events + workouts, now.tzinfo
            )
            budget = min(
                planning["daily_focus_minutes"],
                self.calendar_service._interval_minutes(free),
            )
            while budget >= 30:
                progressed = False
                for item in candidates:
                    if item["minutes"] <= 0 or budget < 30:
                        continue
                    preferred = min(
                        planning["large_task_block_minutes"],
                        item["minutes"],
                        budget,
                    )
                    slot = self.calendar_service._take_flexible_slot(
                        free, preferred, minimum_minutes=min(30, preferred)
                    )
                    if slot is None:
                        continue
                    start_at, end_at = slot
                    minutes = int((end_at - start_at).total_seconds() // 60)
                    item["minutes"] -= minutes
                    budget -= minutes
                    proposal.append({
                        "task": item["task"],
                        "date": day,
                        "start": start_at,
                        "end": end_at,
                        "minutes": minutes,
                    })
                    progressed = True
                if not progressed:
                    break

        unallocated = [item for item in candidates if item["minutes"] > 0]
        return {
            "today": today,
            "proposal": proposal,
            "unallocated": unallocated,
            "planning": planning,
            "skipped_rest_days": skipped_rest_days,
            "timezone": getattr(now.tzinfo, "key", str(now.tzinfo)),
        }

    async def apply_overflow_proposal(self, session, user_id: int, now: datetime) -> dict:
        data = await self.build_overflow_proposal(session, user_id, now)
        await session.execute(
            delete(TaskPlanBlock).where(
                TaskPlanBlock.user_id == user_id,
                TaskPlanBlock.plan_date > now.date(),
                TaskPlanBlock.status == "planned",
                TaskPlanBlock.source == "assistant_overflow",
            )
        )
        for item in data["proposal"]:
            session.add(TaskPlanBlock(
                user_id=user_id,
                task_id=item["task"].id,
                plan_date=item["date"],
                start_at=item["start"],
                end_at=item["end"],
                planned_minutes=item["minutes"],
            ))
        await session.flush()
        data["saved"] = len(data["proposal"])
        return data

    @staticmethod
    def render_proposal(data: dict, *, saved: bool = False) -> str:
        proposal = data["proposal"]
        if not proposal:
            return (
                "Перенос не нужен\n\n"
                "Все готовые задачи уже помещаются в выбранный дневной лимит."
            )
        heading = "Остаток распределён" if saved else "Предлагаю разнести остаток"
        lines = [
            heading,
            "",
            f"Часовой пояс: {data['timezone']}",
            f"Дневной фокус: до {data['planning']['daily_focus_minutes'] // 60} ч.",
            "Дни: каждый день." if data["planning"]["planning_weekends"]
            else "Дни: понедельник–пятница.",
            "",
        ]
        current_day = None
        weekdays = (
            "понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье",
        )
        for item in proposal[:18]:
            if item["date"] != current_day:
                current_day = item["date"]
                lines += [
                    f"{current_day.strftime('%d.%m')}, {weekdays[current_day.weekday()]}:"
                ]
            lines.append(
                f"• {item['start'].strftime('%H:%M')}–{item['end'].strftime('%H:%M')} · "
                f"{escape(item['task'].title)}"
            )
        if len(proposal) > 18:
            lines += ["", f"И ещё рабочих блоков: {len(proposal) - 18}."]
        if data["skipped_rest_days"]:
            lines += [
                "",
                f"Не ставил задачи в выходные/дни отдыха: {len(data['skipped_rest_days'])}.",
            ]
        if data["unallocated"]:
            total = sum(item["minutes"] for item in data["unallocated"])
            lines += [
                "",
                f"За {30} дней всё ещё не распределено: {total // 60} ч {total % 60} мин.",
                "Нужно увеличить горизонт, снизить объём или разбить проект точнее.",
            ]
        if saved:
            lines += [
                "",
                "План сохранён отдельными рабочими блоками. Полная оценка задач не уменьшена.",
            ]
        else:
            lines += [
                "",
                "Это предложение. Пока ты не подтвердил, расписание не сохраняется.",
            ]
        return "\n".join(lines)
