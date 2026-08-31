from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from typing import ClassVar

from sqlalchemy import delete, select

from bot.database.models import (
    HealthSnapshot,
    Reminder,
    TrainingProfile,
    WorkoutPlan,
    WorkoutSession,
    WorkoutSetLog,
)
from bot.services.datetime_utils import ensure_aware


class TrainingService:
    DAY_MAP: ClassVar[dict[str, int]] = {
        "пн": 0, "понедельник": 0, "mon": 0,
        "вт": 1, "вторник": 1, "tue": 1,
        "ср": 2, "среда": 2, "wed": 2,
        "чт": 3, "четверг": 3, "thu": 3,
        "пт": 4, "пятница": 4, "fri": 4,
        "сб": 5, "суббота": 5, "sat": 5,
        "вс": 6, "воскресенье": 6, "sun": 6,
    }

    def __init__(self, calendar_service=None):
        self.calendar_service = calendar_service

    async def get_profile(self, session, user_id: int) -> TrainingProfile | None:
        result = await session.execute(
            select(TrainingProfile).where(TrainingProfile.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_workout_context(
        self,
        session,
        user_id: int,
        workout_id: int,
    ) -> tuple[WorkoutPlan, list[WorkoutSession], WorkoutSession]:
        result = await session.execute(
            select(WorkoutSession).where(
                WorkoutSession.id == workout_id,
                WorkoutSession.user_id == user_id,
            )
        )
        workout = result.scalar_one_or_none()
        if workout is None or workout.plan_id is None:
            raise ValueError("Тренировочная неделя не найдена")
        plan_result = await session.execute(
            select(WorkoutPlan).where(
                WorkoutPlan.id == workout.plan_id,
                WorkoutPlan.user_id == user_id,
            )
        )
        plan = plan_result.scalar_one_or_none()
        if plan is None:
            raise ValueError("Тренировочная неделя не найдена")
        sessions_result = await session.execute(
            select(WorkoutSession)
            .where(
                WorkoutSession.plan_id == plan.id,
                WorkoutSession.user_id == user_id,
            )
            .order_by(WorkoutSession.scheduled_for, WorkoutSession.id)
        )
        return plan, list(sessions_result.scalars().all()), workout

    async def save_profile(self, session, user_id: int, raw: str) -> TrainingProfile:
        fields = self._parse_fields(raw)
        required = ["goal", "experience", "equipment", "limitations", "days", "minutes"]
        missing = [key for key in required if not fields.get(key)]
        if missing:
            raise ValueError("Не заполнено: " + ", ".join(missing))
        profile = await self.get_profile(session, user_id)
        if profile is None:
            profile = TrainingProfile(user_id=user_id)
            session.add(profile)
        profile.goal = fields["goal"][:1000]
        profile.experience = self._experience(fields["experience"])
        profile.equipment = fields["equipment"][:1000]
        profile.limitations = fields["limitations"][:1000]
        profile.focus_areas = fields.get("focus", fields["goal"])[:1000]
        days, preferred_times = self.parse_schedule(fields["days"])
        fixed_sessions = self.parse_fixed_sessions(fields.get("fixed", "нет"))
        if not days and not fixed_sessions:
            raise ValueError("Нужен хотя бы один день для тренировки или фиксированного занятия")
        profile.preferred_days_json = json.dumps(days)
        profile.preferred_times_json = json.dumps(preferred_times)
        profile.fixed_sessions_json = json.dumps(fixed_sessions, ensure_ascii=False)
        profile.days_per_week = len(days)
        profile.session_minutes = max(15, min(120, int(re.search(r"\d+", fields["minutes"]).group())))
        profile.active = True
        await session.flush()
        return profile

    async def generate_week(self, session, user_id: int, now: datetime) -> tuple[WorkoutPlan, list[WorkoutSession], list[Reminder]]:
        profile = await self.get_profile(session, user_id)
        if profile is None:
            raise ValueError("Сначала заполни /training_setup")
        if self._has_high_risk_limitations(profile.limitations):
            raise ValueError(
                "В ограничениях указана боль, травма или сердечно-сосудистый риск. "
                "Нужна очная рекомендация врача/физиотерапевта перед автопланом."
            )

        old = await session.execute(
            select(WorkoutPlan).where(
                WorkoutPlan.user_id == user_id,
                WorkoutPlan.status == "active",
            ).order_by(WorkoutPlan.week_start.desc(), WorkoutPlan.id.desc())
        )
        for existing in old.scalars().all():
            sessions_result = await session.execute(
                select(WorkoutSession).where(WorkoutSession.plan_id == existing.id)
            )
            existing_sessions = list(sessions_result.scalars().all())
            if any(
                item.status in {"planned", "in_progress"}
                and ensure_aware(item.scheduled_for, now.tzinfo) >= now
                for item in existing_sessions
            ):
                return existing, existing_sessions, []
            existing.status = "completed" if all(
                item.status == "completed" for item in existing_sessions
            ) else "expired"

        modifier, reason = await self._adjustment(session, user_id, now)
        days = json.loads(profile.preferred_days_json or "[]")[: profile.days_per_week]
        preferred_times = json.loads(profile.preferred_times_json or "{}")
        fixed_sessions = json.loads(profile.fixed_sessions_json or "[]")
        exercises = self._exercise_template(
            profile.equipment,
            profile.experience,
            profile.goal,
            profile.focus_areas,
            profile.session_minutes,
            modifier,
            len(days),
        )
        exercise_history = await self._exercise_history(session, user_id, now)
        self._apply_exercise_guidance(exercises, exercise_history)
        week_start = now.date()
        plan_data = {
            "goal": profile.goal,
            "experience": profile.experience,
            "session_minutes": profile.session_minutes,
            "days": days,
            "preferred_times": preferred_times,
            "fixed_sessions": fixed_sessions,
            "sessions": exercises,
            "safety": "Остановись при острой боли, головокружении или необычной одышке.",
        }
        if fixed_sessions:
            reason += (
                f" Учтено фиксированных занятий: {len(fixed_sessions)}; "
                "дополнительная нагрузка рядом с тяжёлыми занятиями облегчается."
            )
        plan = WorkoutPlan(
            user_id=user_id,
            week_start=week_start,
            title=f"Ближайшие тренировки · с {week_start.isoformat()}",
            plan_json=json.dumps(plan_data, ensure_ascii=False),
            adjustment_reason=reason,
        )
        session.add(plan)
        await session.flush()

        sessions: list[WorkoutSession] = []
        reminders: list[Reminder] = []
        scheduled_dates: list[tuple[datetime, dict]] = []
        for weekday in days:
            days_ahead = (int(weekday) - now.weekday()) % 7
            scheduled_date = now.date() + timedelta(days=days_ahead)
            hour, minute = self._clock(preferred_times.get(str(weekday), "18:30"))
            scheduled_for = datetime.combine(
                scheduled_date,
                time(hour, minute),
                tzinfo=now.tzinfo,
            )
            if scheduled_for <= now:
                scheduled_for += timedelta(days=7)
            if self.calendar_service:
                scheduled_for = await self.calendar_service.find_free_start(
                    session, user_id, scheduled_for, profile.session_minutes
                )
            scheduled_dates.append((scheduled_for, {"kind": "generated"}))

        for fixed in fixed_sessions:
            weekday = int(fixed["weekday"])
            days_ahead = (weekday - now.weekday()) % 7
            scheduled_date = now.date() + timedelta(days=days_ahead)
            hour, minute = self._clock(fixed.get("time", "18:30"))
            scheduled_for = datetime.combine(
                scheduled_date,
                time(hour, minute),
                tzinfo=now.tzinfo,
            )
            if scheduled_for <= now:
                scheduled_for += timedelta(days=7)
            scheduled_dates.append((scheduled_for, {"kind": "fixed", **fixed}))

        generated_index = 0
        for scheduled_for, source_data in sorted(scheduled_dates, key=lambda item: item[0]):
            if source_data["kind"] == "fixed":
                workout_data = {
                    "title": source_data["title"],
                    "focus": source_data.get("focus", "фиксированное занятие"),
                    "estimated_minutes": source_data.get("minutes", profile.session_minutes),
                    "warmup_minutes": 0,
                    "exercises": [],
                    "cooldown_minutes": 0,
                    "activity_type": source_data["activity_type"],
                    "load_level": source_data["load_level"],
                    "muscle_groups": source_data["muscle_groups"],
                }
            else:
                workout_data = json.loads(
                    json.dumps(exercises[generated_index % len(exercises)], ensure_ascii=False)
                )
                generated_index += 1
                nearby_fixed = [
                    (fixed_time, fixed_data)
                    for fixed_time, fixed_data in scheduled_dates
                    if fixed_data["kind"] == "fixed"
                    and abs((fixed_time - scheduled_for).total_seconds()) < 36 * 3600
                ]
                if any(
                    self._load_rank(fixed_data["load_level"]) >= 3
                    for _, fixed_data in nearby_fixed
                ):
                    workout_data["load_level"] = "light"
                    workout_data["focus"] += "; облегчено рядом с тяжёлым занятием"
                    for exercise in workout_data.get("exercises", []):
                        exercise["sets"] = max(1, int(exercise.get("sets", 2)) - 1)
            workout = WorkoutSession(
                user_id=user_id,
                plan_id=plan.id,
                scheduled_for=scheduled_for,
                title=workout_data["title"],
                details_json=json.dumps(workout_data, ensure_ascii=False),
                activity_type=workout_data.get("activity_type", "strength"),
                load_level=workout_data.get("load_level", "medium"),
                muscle_groups_json=json.dumps(workout_data.get("muscle_groups", [])),
                is_fixed=source_data["kind"] == "fixed",
                source=source_data["kind"],
            )
            session.add(workout)
            await session.flush()
            sessions.append(workout)
            reminder = Reminder(
                user_id=user_id,
                text=f"Тренировка: {workout.title}",
                remind_at=scheduled_for,
                priority="medium",
                related_entity_type="workout_session",
                related_entity_id=workout.id,
                recurrence="none",
            )
            session.add(reminder)
            await session.flush()
            reminders.append(reminder)
        await session.flush()
        return plan, sessions, reminders

    async def complete_nearest(self, session, user_id: int, now: datetime, rpe: int, notes: str) -> WorkoutSession:
        target = await self.nearest_planned(session, user_id, now)
        await self.complete_workout(session, user_id, target.id, now, rpe, notes)
        return target

    async def nearest_planned(
        self,
        session,
        user_id: int,
        now: datetime,
    ) -> WorkoutSession:
        result = await session.execute(
            select(WorkoutSession).where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.status.in_(["planned", "in_progress"]),
            )
        )
        candidates = list(result.scalars().all())
        if not candidates:
            raise ValueError("Нет запланированной тренировки")
        return min(
            candidates,
            key=lambda item: abs(
                (ensure_aware(item.scheduled_for, now.tzinfo) - now).total_seconds()
            ),
        )

    async def complete_workout(
        self,
        session,
        user_id: int,
        workout_id: int,
        now: datetime,
        rpe: int,
        notes: str = "",
    ) -> tuple[WorkoutSession, Reminder | None]:
        target = await self._workout_with_status(
            session, user_id, workout_id, {"planned", "in_progress"}
        )
        target.status = "completed"
        target.rpe = max(1, min(10, int(rpe)))
        target.notes = notes[:1000]
        target.completed_at = now
        reminder = await self._linked_reminder(session, user_id, target.id)
        if reminder:
            reminder.status = "done"
        await session.flush()
        if target.plan_id is not None:
            await self.adapt_remaining_workouts(
                session,
                user_id,
                target.plan_id,
                now,
            )
        return target, reminder

    async def reschedule_workout(
        self,
        session,
        user_id: int,
        workout_id: int,
        new_time: datetime,
        now: datetime,
    ) -> tuple[WorkoutSession, Reminder | None]:
        target = await self._planned_workout(session, user_id, workout_id)
        aware_time = ensure_aware(new_time, now.tzinfo)
        if aware_time <= now:
            raise ValueError("Новое время тренировки должно быть в будущем")
        if target.rescheduled_from is None:
            target.rescheduled_from = target.scheduled_for
        target.scheduled_for = aware_time
        reminder = await self._linked_reminder(session, user_id, target.id)
        if reminder:
            reminder.remind_at = aware_time
            reminder.status = "active"
        await session.flush()
        return target, reminder

    async def reschedule_and_rebalance(
        self,
        session,
        user_id: int,
        workout_id: int,
        new_time: datetime,
        now: datetime,
    ) -> tuple[
        WorkoutSession,
        Reminder | None,
        list[tuple[WorkoutSession, Reminder | None]],
        list[str],
    ]:
        target, target_reminder = await self.reschedule_workout(
            session, user_id, workout_id, new_time, now
        )
        if target.plan_id is None:
            return target, target_reminder, [], []

        result = await session.execute(
            select(WorkoutSession)
            .where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.plan_id == target.plan_id,
                WorkoutSession.id != target.id,
                WorkoutSession.status == "planned",
            )
            .order_by(WorkoutSession.scheduled_for, WorkoutSession.id)
        )
        remaining = list(result.scalars().all())
        anchors = [target, *(item for item in remaining if item.is_fixed)]
        flexible = [item for item in remaining if not item.is_fixed]
        changed: list[tuple[WorkoutSession, Reminder | None]] = []
        warnings: list[str] = []

        for item in flexible:
            original = ensure_aware(item.scheduled_for, now.tzinfo)
            chosen: datetime | None = None
            for offset in range(8):
                candidate = original + timedelta(days=offset)
                if candidate <= now:
                    continue
                if self.calendar_service:
                    details = json.loads(item.details_json or "{}")
                    candidate = await self.calendar_service.find_free_start(
                        session,
                        user_id,
                        candidate,
                        int(details.get("estimated_minutes", 45)),
                    )
                if not self._has_schedule_conflict(item, candidate, anchors, now):
                    chosen = candidate
                    break
            if chosen is None:
                warnings.append(
                    f"для «{item.title}» не найдено свободного окна без конфликта в ближайшие 7 дней"
                )
                anchors.append(item)
                continue
            if chosen != original:
                if item.rescheduled_from is None:
                    item.rescheduled_from = item.scheduled_for
                item.scheduled_for = chosen
                reminder = await self._linked_reminder(session, user_id, item.id)
                if reminder:
                    reminder.remind_at = chosen
                    reminder.status = "active"
                changed.append((item, reminder))
            anchors.append(item)
        await session.flush()
        return target, target_reminder, changed, warnings

    async def skip_workout(
        self,
        session,
        user_id: int,
        workout_id: int,
        reason: str = "",
    ) -> tuple[WorkoutSession, Reminder | None]:
        target = await self._planned_workout(session, user_id, workout_id)
        target.status = "skipped"
        target.skip_reason = reason[:1000]
        reminder = await self._linked_reminder(session, user_id, target.id)
        if reminder:
            reminder.status = "cancelled"
        await session.flush()
        return target, reminder

    async def start_workout(
        self,
        session,
        user_id: int,
        workout_id: int,
        now: datetime,
    ) -> tuple[WorkoutSession, Reminder | None]:
        target = await self._planned_workout(session, user_id, workout_id)
        target.status = "in_progress"
        target.started_at = now
        target.current_exercise_index = 0
        reminder = await self._linked_reminder(session, user_id, target.id)
        if reminder:
            reminder.status = "done"
        await session.flush()
        return target, reminder

    async def record_current_exercise(
        self,
        session,
        user_id: int,
        workout_id: int,
        entries: list[tuple[int, str]],
        now: datetime,
    ) -> tuple[WorkoutSession, dict | None]:
        target = await self._workout_with_status(
            session, user_id, workout_id, {"in_progress"}
        )
        exercise = self.current_exercise(target)
        if exercise is None:
            raise ValueError("В этой тренировке больше нет упражнений")
        index = target.current_exercise_index
        await session.execute(
            delete(WorkoutSetLog).where(
                WorkoutSetLog.user_id == user_id,
                WorkoutSetLog.workout_session_id == target.id,
                WorkoutSetLog.exercise_index == index,
            )
        )
        for set_number, (reps, weight_kg) in enumerate(entries, 1):
            session.add(WorkoutSetLog(
                user_id=user_id,
                workout_session_id=target.id,
                exercise_index=index,
                exercise_name=str(exercise.get("exercise", "Упражнение"))[:255],
                set_number=set_number,
                reps=reps,
                weight_kg=weight_kg,
                completed_at=now,
            ))
        target.current_exercise_index += 1
        await session.flush()
        return target, self.current_exercise(target)

    async def skip_current_exercise(
        self,
        session,
        user_id: int,
        workout_id: int,
    ) -> tuple[WorkoutSession, dict | None]:
        target = await self._workout_with_status(
            session, user_id, workout_id, {"in_progress"}
        )
        if self.current_exercise(target) is None:
            raise ValueError("В этой тренировке больше нет упражнений")
        target.current_exercise_index += 1
        await session.flush()
        return target, self.current_exercise(target)

    async def get_set_logs(
        self,
        session,
        user_id: int,
        workout_id: int,
    ) -> list[WorkoutSetLog]:
        result = await session.execute(
            select(WorkoutSetLog)
            .where(
                WorkoutSetLog.user_id == user_id,
                WorkoutSetLog.workout_session_id == workout_id,
            )
            .order_by(WorkoutSetLog.exercise_index, WorkoutSetLog.set_number)
        )
        return list(result.scalars().all())

    async def adapt_remaining_workouts(
        self,
        session,
        user_id: int,
        plan_id: int,
        now: datetime,
    ) -> int:
        history = await self._exercise_history(session, user_id, now)
        result = await session.execute(
            select(WorkoutSession).where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.plan_id == plan_id,
                WorkoutSession.status == "planned",
                WorkoutSession.scheduled_for > now,
                WorkoutSession.is_fixed.is_(False),
            )
        )
        changed = 0
        for workout in result.scalars().all():
            details = json.loads(workout.details_json or "{}")
            before = json.dumps(details, ensure_ascii=False, sort_keys=True)
            self._apply_exercise_guidance([details], history)
            after = json.dumps(details, ensure_ascii=False, sort_keys=True)
            if after != before:
                workout.details_json = json.dumps(details, ensure_ascii=False)
                changed += 1
        await session.flush()
        return changed

    async def _exercise_history(
        self,
        session,
        user_id: int,
        now: datetime,
    ) -> dict[str, list[dict]]:
        start = now - timedelta(days=56)
        result = await session.execute(
            select(
                WorkoutSetLog,
                WorkoutSession.id,
                WorkoutSession.rpe,
                WorkoutSession.completed_at,
            )
            .join(
                WorkoutSession,
                WorkoutSession.id == WorkoutSetLog.workout_session_id,
            )
            .where(
                WorkoutSetLog.user_id == user_id,
                WorkoutSession.user_id == user_id,
                WorkoutSession.status == "completed",
                WorkoutSession.completed_at >= start,
            )
            .order_by(
                WorkoutSession.completed_at.desc(),
                WorkoutSetLog.set_number,
            )
        )
        grouped: dict[str, dict[int, dict]] = {}
        for log, session_id, rpe, completed_at in result.all():
            name = log.exercise_name.strip().casefold()
            sessions = grouped.setdefault(name, {})
            exposure = sessions.setdefault(
                session_id,
                {
                    "completed_at": completed_at,
                    "rpe": rpe,
                    "reps": [],
                    "weights": [],
                },
            )
            exposure["reps"].append(int(log.reps))
            if log.weight_kg:
                try:
                    exposure["weights"].append(float(log.weight_kg))
                except ValueError:
                    pass
        return {
            name: sorted(
                sessions.values(),
                key=lambda item: item["completed_at"],
                reverse=True,
            )
            for name, sessions in grouped.items()
        }

    def _apply_exercise_guidance(
        self,
        workouts: list[dict],
        history: dict[str, list[dict]],
    ) -> None:
        for workout in workouts:
            for exercise in workout.get("exercises", []):
                exposures = history.get(str(exercise.get("exercise", "")).casefold())
                if not exposures:
                    continue
                rep_numbers = [int(value) for value in re.findall(r"\d+", str(exercise.get("reps", "")))]
                low = rep_numbers[0] if rep_numbers else 1
                high = rep_numbers[-1] if rep_numbers else 100
                latest = exposures[0]
                recent = exposures[:2]
                increase = len(recent) >= 2 and all(
                    item["reps"]
                    and min(item["reps"]) >= high
                    and (item["rpe"] or 8) <= 7
                    for item in recent
                )
                reduce = bool(
                    (latest["rpe"] or 0) >= 9
                    or (latest["reps"] and min(latest["reps"]) < low)
                )
                last_weight = max(latest["weights"], default=None)
                reps_label = "/".join(str(value) for value in latest["reps"])
                last_result = f"{len(latest['reps'])} подх. · {reps_label} повт."
                if last_weight is not None:
                    last_result += f" · до {self._format_weight(last_weight)} кг"
                exercise["last_result"] = last_result

                if increase:
                    exercise["progression"] = "increase"
                    if last_weight is None:
                        exercise["progression_note"] = (
                            "Верх диапазона выполнен дважды — добавь 1–2 повтора "
                            "или выбери чуть более сложный вариант."
                        )
                    else:
                        suggested = last_weight + self._weight_increment(last_weight)
                        exercise["suggested_weight_kg"] = self._format_weight(suggested)
                        exercise["progression_note"] = (
                            "Верх диапазона выполнен дважды с умеренной нагрузкой — "
                            "можно немного увеличить вес."
                        )
                elif reduce:
                    exercise["progression"] = "reduce"
                    if last_weight is None:
                        exercise["progression_note"] = (
                            "Прошлый подход был тяжёлым — оставь запас и выбери более лёгкий вариант."
                        )
                    else:
                        suggested = max(0.5, round(last_weight * 0.95 * 2) / 2)
                        exercise["suggested_weight_kg"] = self._format_weight(suggested)
                        exercise["progression_note"] = (
                            "Прошлая тренировка была тяжёлой — вес немного снижен."
                        )
                else:
                    exercise["progression"] = "hold"
                    if last_weight is not None:
                        exercise["suggested_weight_kg"] = self._format_weight(last_weight)
                    exercise["progression_note"] = "Сохрани прошлую нагрузку и чистую технику."

    @staticmethod
    def _weight_increment(weight: float) -> float:
        if weight <= 10:
            return 0.5
        if weight <= 30:
            return 1.0
        if weight <= 60:
            return 2.5
        return 5.0

    @staticmethod
    def _format_weight(weight: float) -> str:
        return f"{weight:g}"

    async def analyze_reschedule(
        self,
        session,
        user_id: int,
        workout_id: int,
        new_time: datetime,
        now: datetime,
    ) -> list[str]:
        target = await self._planned_workout(session, user_id, workout_id)
        aware_time = ensure_aware(new_time, now.tzinfo)
        result = await session.execute(
            select(WorkoutSession).where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.id != workout_id,
                WorkoutSession.status.in_(["planned", "in_progress"]),
            )
        )
        target_groups = self._session_muscle_groups(target)
        target_rank = self._load_rank(target.load_level)
        conflicts: list[str] = []
        for other in result.scalars().all():
            other_time = ensure_aware(other.scheduled_for, now.tzinfo)
            hours = abs((other_time - aware_time).total_seconds()) / 3600
            if other.is_fixed and other_time.date() == aware_time.date():
                conflicts.append(
                    f"в этот день уже есть фиксированное занятие «{other.title}»"
                )
            if hours < 2:
                conflicts.append(
                    f"время пересекается с тренировкой «{other.title}»"
                )
            if hours < 18 and target_rank >= 2 and self._load_rank(other.load_level) >= 2:
                conflicts.append(
                    f"между «{target.title}» и «{other.title}» останется меньше 18 часов"
                )
                continue
            overlap = self._overlapping_groups(
                target_groups,
                self._session_muscle_groups(other),
            )
            if hours < 36 and overlap and target_rank >= 2 and self._load_rank(other.load_level) >= 2:
                group_labels = {
                    "back": "спина",
                    "biceps": "бицепс",
                    "cardio": "выносливость",
                    "chest": "грудь",
                    "core": "корпус",
                    "full_body": "всё тело",
                    "glutes": "ягодицы",
                    "legs": "ноги",
                    "mobility": "мобилити",
                    "shoulders": "плечи",
                    "triceps": "трицепс",
                }
                readable_groups = ", ".join(
                    group_labels.get(group, group) for group in sorted(overlap)
                )
                conflicts.append(
                    f"зоны {readable_groups} получат повторную нагрузку раньше 36 часов"
                )
        return list(dict.fromkeys(conflicts))

    def _has_schedule_conflict(
        self,
        workout: WorkoutSession,
        candidate: datetime,
        anchors: list[WorkoutSession],
        now: datetime,
    ) -> bool:
        target_groups = self._session_muscle_groups(workout)
        target_rank = self._load_rank(workout.load_level)
        for other in anchors:
            other_time = ensure_aware(other.scheduled_for, now.tzinfo)
            hours = abs((other_time - candidate).total_seconds()) / 3600
            if other.is_fixed and other_time.date() == candidate.date():
                return True
            if hours < 2:
                return True
            other_rank = self._load_rank(other.load_level)
            if hours < 18 and target_rank >= 2 and other_rank >= 2:
                return True
            if (
                hours < 36
                and target_rank >= 2
                and other_rank >= 2
                and self._overlapping_groups(
                    target_groups,
                    self._session_muscle_groups(other),
                )
            ):
                return True
        return False

    @staticmethod
    def current_exercise(workout: WorkoutSession) -> dict | None:
        details = json.loads(workout.details_json or "{}")
        exercises = list(details.get("exercises") or [])
        index = max(0, workout.current_exercise_index or 0)
        return exercises[index] if index < len(exercises) else None

    @staticmethod
    def parse_set_entries(raw: str) -> list[tuple[int, str]]:
        entries: list[tuple[int, str]] = []
        for token in re.split(r"[,;\n]+", raw.lower().strip()):
            token = token.strip().replace("кг", "").strip()
            if not token:
                continue
            match = re.fullmatch(r"(\d{1,3})\s*[xх×*]\s*(\d{1,3}(?:\.\d)?)", token)
            if match:
                reps = int(match.group(1))
                weight = match.group(2)
            elif re.fullmatch(r"\d{1,3}", token):
                reps = int(token)
                weight = ""
            else:
                raise ValueError("Формат подходов: 10x40, 10x40, 8x40 — или 12, 12, 10 без веса")
            if not 1 <= reps <= 100 or (weight and float(weight) > 1000):
                raise ValueError("Проверь повторения и вес в подходах")
            entries.append((reps, weight))
        if not entries or len(entries) > 10:
            raise ValueError("Укажи от 1 до 10 подходов")
        return entries

    async def _planned_workout(
        self,
        session,
        user_id: int,
        workout_id: int,
    ) -> WorkoutSession:
        return await self._workout_with_status(
            session, user_id, workout_id, {"planned"}
        )

    async def _workout_with_status(
        self,
        session,
        user_id: int,
        workout_id: int,
        statuses: set[str],
    ) -> WorkoutSession:
        result = await session.execute(
            select(WorkoutSession).where(
                WorkoutSession.id == workout_id,
                WorkoutSession.user_id == user_id,
                WorkoutSession.status.in_(statuses),
            )
        )
        target = result.scalar_one_or_none()
        if target is None:
            raise ValueError("Тренировка не найдена или уже обработана")
        return target

    async def _linked_reminder(
        self,
        session,
        user_id: int,
        workout_id: int,
    ) -> Reminder | None:
        result = await session.execute(
            select(Reminder)
            .where(
                Reminder.user_id == user_id,
                Reminder.related_entity_type == "workout_session",
                Reminder.related_entity_id == workout_id,
            )
            .order_by(Reminder.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _adjustment(self, session, user_id: int, now: datetime) -> tuple[float, str]:
        health_result = await session.execute(
            select(HealthSnapshot).where(HealthSnapshot.user_id == user_id)
            .order_by(HealthSnapshot.date.desc()).limit(8)
        )
        health = list(health_result.scalars().all())
        if health:
            latest = health[0]
            baseline = health[1:]
            recovery_flags = []
            if latest.sleep_minutes and latest.sleep_minutes < 360:
                recovery_flags.append("сон менее 6 часов")
            rhr_values = [row.resting_heart_rate for row in baseline if row.resting_heart_rate]
            if latest.resting_heart_rate and rhr_values and latest.resting_heart_rate > sum(rhr_values) / len(rhr_values) * 1.12:
                recovery_flags.append("пульс покоя выше личной базы")
            hrv_values = [row.hrv_ms for row in baseline if row.hrv_ms]
            if latest.hrv_ms and hrv_values and latest.hrv_ms < sum(hrv_values) / len(hrv_values) * 0.75:
                recovery_flags.append("HRV ниже личной базы")
            if recovery_flags:
                return 0.8, "Восстановительный объём: " + ", ".join(recovery_flags) + ". Это не диагноз."
        start = now - timedelta(days=21)
        result = await session.execute(
            select(WorkoutSession).where(
                WorkoutSession.user_id == user_id,
                WorkoutSession.scheduled_for >= start,
                WorkoutSession.scheduled_for < now,
            )
        )
        sessions = list(result.scalars().all())
        if not sessions:
            return 1.0, "Первый план: базовый объём."
        completed = [item for item in sessions if item.status == "completed"]
        rate = len(completed) / len(sessions)
        avg_rpe = sum(item.rpe or 7 for item in completed) / len(completed) if completed else 10
        if rate >= 0.8 and avg_rpe <= 7:
            return 1.1, "Выполнение ≥80% и RPE ≤7: небольшой рост объёма."
        if rate < 0.6 or avg_rpe >= 9:
            return 0.8, "Низкое выполнение или высокий RPE: объём снижен."
        return 1.0, "Нагрузка сохранена по фактическому выполнению."

    def _exercise_template(
        self,
        equipment: str,
        experience: str,
        goal: str,
        focus_areas: str,
        session_minutes: int,
        modifier: float,
        sessions_count: int,
    ) -> list[dict]:
        lowered = equipment.lower()
        if any(word in lowered for word in ("зал", "штанг", "тренаж")):
            movements = {
                "squat": "Присед или жим ногами",
                "hinge": "Румынская тяга",
                "single_leg": "Болгарский сплит-присед",
                "push": "Жим лёжа",
                "push_vertical": "Жим гантелей вверх",
                "pull": "Тяга горизонтального блока",
                "pull_vertical": "Тяга верхнего блока",
                "glutes": "Ягодичный мост",
                "core": "Планка или dead bug",
                "carry": "Фермерская прогулка",
            }
        elif any(word in lowered for word in ("гантел", "гир")):
            movements = {
                "squat": "Гоблет-присед",
                "hinge": "Румынская тяга с гантелями",
                "single_leg": "Выпады с гантелями",
                "push": "Жим гантелей лёжа",
                "push_vertical": "Жим гантелей вверх",
                "pull": "Тяга гантели в наклоне",
                "pull_vertical": "Пуловер с гантелью",
                "glutes": "Ягодичный мост с гантелью",
                "core": "Dead bug",
                "carry": "Фермерская прогулка",
            }
        else:
            movements = {
                "squat": "Присед к опоре",
                "hinge": "Наклон-таз назад без веса",
                "single_leg": "Обратные выпады с опорой",
                "push": "Отжимания от опоры",
                "push_vertical": "Пайк-отжимания от высокой опоры",
                "pull": "Тяга резинки или полотенца",
                "pull_vertical": "Тяга резинки сверху",
                "glutes": "Ягодичный мост",
                "core": "Dead bug",
                "carry": "Боковая планка",
            }
        goal_lowered = goal.lower()
        if any(word in goal_lowered for word in ("сил", "мышц", "мас")):
            reps, rest_seconds, focus = "6–10", 120, "сила и техника"
        elif any(word in goal_lowered for word in ("выносл", "похуд", "снизить вес")):
            reps, rest_seconds, focus = "10–15", 60, "общая работоспособность"
        else:
            reps, rest_seconds, focus = "8–12", 90, "общая физическая форма"
        base_sets = 2 if experience == "beginner" else 3
        sets = max(1, min(4, round(base_sets * modifier)))
        warmup = 5 if session_minutes <= 30 else 7
        cooldown = 3 if session_minutes <= 30 else 5
        max_exercises = 4 if session_minutes <= 30 else 5 if session_minutes <= 50 else 6

        if sessions_count <= 3:
            split = [
                ("Всё тело A · ноги + жим", ["squat", "push", "pull", "hinge", "core"]),
                ("Всё тело B · тяга + задняя цепь", ["hinge", "pull_vertical", "push_vertical", "single_leg", "core"]),
                ("Всё тело C · баланс + корпус", ["single_leg", "push", "pull", "glutes", "carry"]),
            ]
        elif sessions_count == 4:
            split = [
                ("Верх A · жим + тяга", ["push", "pull", "push_vertical", "pull_vertical", "core"]),
                ("Низ A · присед", ["squat", "hinge", "single_leg", "glutes", "core"]),
                ("Верх B · спина + плечи", ["pull_vertical", "push_vertical", "pull", "push", "carry"]),
                ("Низ B · задняя цепь", ["hinge", "single_leg", "squat", "glutes", "core"]),
            ]
        else:
            split = [
                ("Жим · грудь + плечи", ["push", "push_vertical", "core", "carry"]),
                ("Тяга · спина", ["pull", "pull_vertical", "hinge", "core"]),
                ("Ноги · присед", ["squat", "single_leg", "glutes", "core"]),
                ("Верх · баланс", ["push", "pull", "push_vertical", "pull_vertical", "carry"]),
                ("Низ · задняя цепь", ["hinge", "single_leg", "squat", "glutes", "core"]),
                ("Всё тело · лёгкая техника", ["squat", "push", "pull", "hinge", "core"]),
            ]

        user_focus = (focus_areas.strip() or "равномерное развитие")[:120]
        result = []
        for title, movement_keys in split[:sessions_count]:
            muscle_groups = sorted({
                group
                for key in movement_keys
                for group in self._movement_groups(key)
            })
            body = [
                {
                    "exercise": movements[key],
                    "sets": sets,
                    "reps": reps,
                    "target_rpe": "6–8",
                    "rest_seconds": rest_seconds,
                }
                for key in movement_keys[:max_exercises]
            ]
            result.append({
                "title": title,
                "focus": f"{focus}; приоритет: {user_focus}",
                "estimated_minutes": session_minutes,
                "warmup_minutes": warmup,
                "exercises": body,
                "cooldown_minutes": cooldown,
                "activity_type": "strength",
                "load_level": "light" if "лёгкая" in title.lower() else "medium",
                "muscle_groups": muscle_groups,
            })
        return result

    @staticmethod
    def _parse_fields(raw: str) -> dict[str, str]:
        aliases = {
            "цель": "goal", "goal": "goal", "уровень": "experience", "опыт": "experience",
            "experience": "experience", "оборудование": "equipment", "equipment": "equipment",
            "ограничения": "limitations", "limitations": "limitations", "дни": "days", "days": "days",
            "минуты": "minutes", "длительность": "minutes", "minutes": "minutes",
            "фокус": "focus", "приоритет": "focus", "focus": "focus",
            "фиксированные": "fixed", "фикс": "fixed", "fixed": "fixed",
        }
        result: dict[str, str] = {}
        key_pattern = "|".join(
            re.escape(key) for key in sorted(aliases, key=len, reverse=True)
        )
        markers = list(re.finditer(rf"(?i)(?<!\w)({key_pattern})\s*[=:]", raw))
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(raw)
            normalized = aliases[marker.group(1).lower()]
            value = raw[marker.end():end].strip(" ;\n\t")
            if value:
                result[normalized] = value
        return result

    def parse_schedule(self, raw: str) -> tuple[list[int], dict[str, str]]:
        """Parse ``пн 19:00, ср 18:30`` while keeping legacy day-only input."""
        if raw.lower().strip() in {"нет", "none", "не нужны", "только фиксированные"}:
            return [], {}
        found: list[int] = []
        times: dict[str, str] = {}
        for part in re.split(r"[,;/\n]+", raw.lower()):
            weekdays: list[int] = []
            for token in re.findall(r"[а-яёa-z]+", part):
                if token in self.DAY_MAP and self.DAY_MAP[token] not in weekdays:
                    weekdays.append(self.DAY_MAP[token])
            if not weekdays:
                continue
            match = re.search(r"(?<!\d)([01]?\d|2[0-3])[.:]([0-5]\d)(?!\d)", part)
            for weekday in weekdays:
                if weekday not in found:
                    found.append(weekday)
                if match:
                    times[str(weekday)] = (
                        f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
                    )
        if not found:
            raise ValueError("Не удалось распознать дни недели")
        days = sorted(found[:6])
        return days, {str(day): times[str(day)] for day in days if str(day) in times}

    def parse_fixed_sessions(self, raw: str) -> list[dict]:
        normalized = raw.lower().strip()
        if normalized in {"", "нет", "none", "без фиксированных"}:
            return []
        sessions = []
        for part in re.split(r"[;\n]+", raw):
            lowered = part.lower().strip()
            if not lowered:
                continue
            weekday = next(
                (value for token, value in self.DAY_MAP.items() if re.search(rf"\b{re.escape(token)}\b", lowered)),
                None,
            )
            if weekday is None:
                raise ValueError(f"Не найден день у фиксированного занятия: {part.strip()}")
            clock_match = re.search(r"(?<!\d)([01]?\d|2[0-3])[.:]([0-5]\d)(?!\d)", lowered)
            clock = (
                f"{int(clock_match.group(1)):02d}:{int(clock_match.group(2)):02d}"
                if clock_match else "18:30"
            )
            activity_type, default_title, groups, default_load = self._activity_profile(lowered)
            if any(word in lowered for word in ("тяж", "высок")):
                load_level = "high"
            elif any(word in lowered for word in ("лёг", "легк", "восстанов")):
                load_level = "light"
            elif "средн" in lowered:
                load_level = "medium"
            else:
                load_level = default_load
            minutes_match = re.search(r"(\d{2,3})\s*мин", lowered)
            load_label = {"light": "лёгкая", "medium": "средняя", "high": "тяжёлая"}[
                load_level
            ]
            sessions.append({
                "title": default_title,
                "weekday": weekday,
                "time": clock,
                "minutes": max(15, min(180, int(minutes_match.group(1)))) if minutes_match else 60,
                "activity_type": activity_type,
                "load_level": load_level,
                "muscle_groups": groups,
                "focus": f"{default_title.lower()} · нагрузка {load_label}",
            })
        return sessions[:7]

    def _parse_days(self, raw: str) -> list[int]:
        days, _ = self.parse_schedule(raw)
        return days

    @staticmethod
    def _clock(raw: str) -> tuple[int, int]:
        match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw.strip())
        if not match:
            return 18, 30
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _movement_groups(key: str) -> list[str]:
        mapping = {
            "squat": ["legs", "glutes"],
            "hinge": ["legs", "glutes", "back"],
            "single_leg": ["legs", "glutes"],
            "push": ["chest", "shoulders", "triceps"],
            "push_vertical": ["shoulders", "triceps"],
            "pull": ["back", "biceps"],
            "pull_vertical": ["back", "biceps"],
            "glutes": ["glutes", "legs"],
            "core": ["core"],
            "carry": ["core", "shoulders"],
        }
        return mapping.get(key, ["full_body"])

    @staticmethod
    def _activity_profile(raw: str) -> tuple[str, str, list[str], str]:
        variants = [
            (("бокс",), "combat", "Бокс", ["full_body", "cardio"], "high"),
            (("борьб",), "combat", "Борьба", ["full_body", "cardio"], "high"),
            (("единобор",), "combat", "Единоборства", ["full_body", "cardio"], "high"),
            (("футбол",), "team_sport", "Футбол", ["legs", "cardio"], "high"),
            (("баскет",), "team_sport", "Баскетбол", ["legs", "cardio"], "high"),
            (("волейбол",), "team_sport", "Волейбол", ["legs", "cardio"], "high"),
            (("хоккей",), "team_sport", "Хоккей", ["legs", "cardio"], "high"),
            (("бег", "пробеж"), "running", "Бег", ["legs", "cardio"], "medium"),
            (("плав",), "swimming", "Плавание", ["full_body", "cardio"], "medium"),
            (("йог", "растяж", "мобил"), "mobility", "Мобилити / растяжка", ["full_body", "mobility"], "light"),
            (("велосип", "сайкл"), "cycling", "Велотренировка", ["legs", "cardio"], "medium"),
        ]
        for keywords, activity_type, title, groups, load in variants:
            if any(keyword in raw for keyword in keywords):
                return activity_type, title, groups, load
        return "other", "Фиксированное занятие", ["full_body"], "medium"

    @staticmethod
    def _load_rank(load_level: str) -> int:
        return {"light": 1, "medium": 2, "high": 3}.get(load_level, 2)

    @staticmethod
    def _session_muscle_groups(workout: WorkoutSession) -> set[str]:
        try:
            groups = json.loads(workout.muscle_groups_json or "[]")
        except json.JSONDecodeError:
            groups = []
        if groups:
            return {str(group) for group in groups}
        details = json.loads(workout.details_json or "{}")
        return {str(group) for group in details.get("muscle_groups", [])}

    @staticmethod
    def _overlapping_groups(left: set[str], right: set[str]) -> set[str]:
        overlap = left & right
        if overlap:
            return overlap
        if "full_body" in left and right:
            return right - {"cardio", "mobility"} or {"full_body"}
        if "full_body" in right and left:
            return left - {"cardio", "mobility"} or {"full_body"}
        return set()

    @staticmethod
    def _experience(raw: str) -> str:
        lowered = raw.lower()
        if any(word in lowered for word in ("нач", "нович", "begin")):
            return "beginner"
        if any(word in lowered for word in ("продвин", "advanced")):
            return "advanced"
        return "intermediate"

    @staticmethod
    def _has_high_risk_limitations(raw: str) -> bool:
        lowered = raw.lower().strip()
        if lowered in {"", "нет", "none", "без ограничений"}:
            return False
        return any(word in lowered for word in ("острая боль", "травм", "серд", "давлен", "обмор", "операц"))
