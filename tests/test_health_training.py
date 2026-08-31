from __future__ import annotations

import json
import unittest
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import (
    Base,
    HealthNudge,
    Reminder,
    WorkoutPlan,
    WorkoutSession,
    WorkoutSetLog,
)
from bot.handlers.operations import _format_training_plan, _workout_target_from_text
from bot.services.health_service import HealthService
from bot.services.time_service import TimeService
from bot.services.training_service import TrainingService


class FixedTimeService(TimeService):
    def now(self):
        return datetime(2026, 7, 31, 18, 0, tzinfo=self.tz)


class RecordingBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    async def send_message(self, user_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.messages.append((user_id, text, reply_markup))


class HealthAndTrainingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        tz = ZoneInfo("Europe/Moscow")
        self.clock = FixedTimeService(tz, time(9), time(14), time(19), time(22))

    async def asyncTearDown(self):
        await self.engine.dispose()

    def health_service(self, bot, user_id=1):
        return HealthService(
            scheduler=None,
            session_factory=self.sessions,
            time_service=self.clock,
            bot=bot,
            user_id=user_id,
            enabled=True,
            nudge_time=time(18),
            default_step_goal=10000,
            min_step_gap=1000,
        )

    async def test_health_nudge_uses_real_gap_and_sends_only_once(self):
        bot = RecordingBot()
        service = self.health_service(bot)
        async with self.sessions() as session:
            await service.upsert_snapshot(
                session,
                1,
                {"date": "2026-07-31", "steps": 6400, "step_goal": 10000},
            )
            await session.commit()

        self.assertTrue(await service.send_step_nudge())
        self.assertFalse(await service.send_step_nudge())
        self.assertIn("6 400 из 10 000", bot.messages[0][1])
        self.assertIn("3 600", bot.messages[0][1])
        async with self.sessions() as session:
            count = await session.scalar(select(func.count()).select_from(HealthNudge))
        self.assertEqual(1, count)

    async def test_failed_health_delivery_does_not_suppress_retry(self):
        service = self.health_service(RecordingBot(fail=True), user_id=2)
        async with self.sessions() as session:
            await service.upsert_snapshot(
                session,
                2,
                {"date": "2026-07-31", "steps": 2000, "step_goal": 10000},
            )
            await session.commit()

        with self.assertRaises(RuntimeError):
            await service.send_step_nudge()
        async with self.sessions() as session:
            count = await session.scalar(
                select(func.count()).select_from(HealthNudge).where(HealthNudge.user_id == 2)
            )
        self.assertEqual(0, count)

    async def test_training_plan_is_idempotent_and_records_rpe(self):
        service = TrainingService()
        async with self.sessions() as session:
            profile = await service.save_profile(
                session,
                1,
                "цель=стать сильнее; уровень=новичок; дни=пн,ср,пт; "
                "минуты=45; оборудование=гантели; ограничения=нет",
            )
            plan, workouts, reminders = await service.generate_week(
                session, 1, self.clock.now()
            )
            await session.commit()

        self.assertEqual(3, profile.days_per_week)
        self.assertEqual(3, len(workouts))
        self.assertEqual(3, len(reminders))
        self.assertIn("Первый план", plan.adjustment_reason)
        first_details = json.loads(workouts[0].details_json)
        self.assertEqual("6–10", first_details["exercises"][0]["reps"])

        async with self.sessions() as session:
            same_plan, same_workouts, new_reminders = await service.generate_week(
                session, 1, self.clock.now()
            )
            completed = await service.complete_nearest(
                session, 1, self.clock.now(), 7, "нормально"
            )
            await session.commit()

        self.assertEqual(plan.id, same_plan.id)
        self.assertEqual(3, len(same_workouts))
        self.assertEqual([], new_reminders)
        self.assertEqual(7, completed.rpe)
        async with self.sessions() as session:
            statuses = set(
                (await session.execute(select(WorkoutSession.status))).scalars().all()
            )
        self.assertIn("completed", statuses)

    async def test_training_plan_rejects_high_risk_limitations(self):
        service = TrainingService()
        async with self.sessions() as session:
            await service.save_profile(
                session,
                3,
                "цель=форма; уровень=новичок; дни=вт,чт; минуты=30; "
                "оборудование=нет; ограничения=травма колена",
            )
            with self.assertRaisesRegex(ValueError, "врача"):
                await service.generate_week(session, 3, self.clock.now())

    async def test_training_plan_uses_preferred_times_and_balanced_sequence(self):
        service = TrainingService()
        async with self.sessions() as session:
            profile = await service.save_profile(
                session,
                4,
                "цель=поддерживать форму; уровень=новичок; "
                "дни=пт 20:15, вс 11:30; минуты=40; "
                "оборудование=гантели; ограничения=нет",
            )
            _plan, workouts, _reminders = await service.generate_week(
                session, 4, self.clock.now()
            )
            await session.commit()

        self.assertEqual({"4": "20:15", "6": "11:30"}, json.loads(profile.preferred_times_json))
        self.assertEqual([20, 11], [item.scheduled_for.hour for item in workouts])
        self.assertEqual([15, 30], [item.scheduled_for.minute for item in workouts])
        self.assertNotEqual(workouts[0].title, workouts[1].title)
        self.assertIn("Всё тело", workouts[0].title)

    async def test_workout_can_be_rescheduled_and_skipped_with_its_reminder(self):
        service = TrainingService()
        now = self.clock.now()
        async with self.sessions() as session:
            await service.save_profile(
                session,
                5,
                "цель=стать сильнее; уровень=новичок; дни=пт 19:00, вс 12:00; "
                "минуты=45; оборудование=зал; ограничения=нет",
            )
            _plan, workouts, reminders = await service.generate_week(session, 5, now)
            first_id = workouts[0].id
            second_id = workouts[1].id
            first_reminder_id = reminders[0].id
            second_reminder_id = reminders[1].id
            moved_to = workouts[0].scheduled_for.replace(hour=21) + timedelta(days=1)
            moved, moved_reminder = await service.reschedule_workout(
                session, 5, first_id, moved_to, now
            )
            skipped, skipped_reminder = await service.skip_workout(
                session, 5, second_id, "Сегодня не могу"
            )
            await session.commit()

        self.assertEqual(moved_to, moved.scheduled_for)
        self.assertEqual(moved_to, moved_reminder.remind_at)
        self.assertEqual("active", moved_reminder.status)
        self.assertEqual("skipped", skipped.status)
        self.assertEqual("Сегодня не могу", skipped.skip_reason)
        self.assertEqual("cancelled", skipped_reminder.status)
        async with self.sessions() as session:
            stored = {
                reminder.id: reminder.status
                for reminder in (
                    await session.execute(
                        select(Reminder).where(
                            Reminder.id.in_([first_reminder_id, second_reminder_id])
                        )
                    )
                ).scalars()
            }
        self.assertEqual("active", stored[first_reminder_id])
        self.assertEqual("cancelled", stored[second_reminder_id])

    async def test_fixed_sessions_are_preserved_and_balance_generated_load(self):
        service = TrainingService()
        async with self.sessions() as session:
            profile = await service.save_profile(
                session,
                6,
                "цель=стать сильнее; уровень=новичок; "
                "фиксированные=бокс вт 19:00 тяжёлая; футбол сб 12:00; "
                "дни=пн 19:00; минуты=45; оборудование=гантели; ограничения=нет",
            )
            plan, workouts, reminders = await service.generate_week(
                session, 6, self.clock.now()
            )
            await session.commit()

        fixed = [item for item in workouts if item.is_fixed]
        generated = [item for item in workouts if not item.is_fixed]
        self.assertEqual(2, len(json.loads(profile.fixed_sessions_json)))
        self.assertEqual({"Бокс", "Футбол"}, {item.title for item in fixed})
        self.assertEqual(3, len(reminders))
        self.assertEqual("light", generated[0].load_level)
        self.assertIn("фиксированных занятий: 2", plan.adjustment_reason)

    async def test_reschedule_analysis_warns_about_fixed_and_recovery_conflicts(self):
        service = TrainingService()
        async with self.sessions() as session:
            await service.save_profile(
                session,
                7,
                "цель=форма; уровень=регулярно; фиксированные=бокс вт 19:00 тяжёлая; "
                "дни=пн 19:00; минуты=45; оборудование=зал; ограничения=нет",
            )
            _plan, workouts, _reminders = await service.generate_week(
                session, 7, self.clock.now()
            )
            generated = next(item for item in workouts if not item.is_fixed)
            fixed = next(item for item in workouts if item.is_fixed)
            generated.load_level = "high"
            conflicts = await service.analyze_reschedule(
                session,
                7,
                generated.id,
                fixed.scheduled_for,
                self.clock.now(),
            )

        self.assertTrue(any("фиксированное занятие" in item for item in conflicts))
        self.assertTrue(any("меньше 18 часов" in item for item in conflicts))

    async def test_guided_workout_records_sets_and_advances_exercises(self):
        service = TrainingService()
        async with self.sessions() as session:
            await service.save_profile(
                session,
                8,
                "цель=сила; уровень=новичок; дни=пн 19:00; минуты=30; "
                "оборудование=гантели; ограничения=нет",
            )
            _plan, workouts, _reminders = await service.generate_week(
                session, 8, self.clock.now()
            )
            workout, reminder = await service.start_workout(
                session, 8, workouts[0].id, self.clock.now()
            )
            entries = service.parse_set_entries("10x20, 10x20, 8x22.5 кг")
            workout, next_exercise = await service.record_current_exercise(
                session, 8, workout.id, entries, self.clock.now()
            )
            logs = await service.get_set_logs(session, 8, workout.id)
            await session.commit()

        self.assertEqual("in_progress", workout.status)
        self.assertEqual("done", reminder.status)
        self.assertEqual(1, workout.current_exercise_index)
        self.assertIsNotNone(next_exercise)
        self.assertEqual([(10, "20"), (10, "20"), (8, "22.5")], entries)
        self.assertEqual(3, len(logs))
        self.assertTrue(all(isinstance(item, WorkoutSetLog) for item in logs))

    async def test_training_plan_screen_only_expands_selected_workout(self):
        service = TrainingService()
        async with self.sessions() as session:
            await service.save_profile(
                session,
                9,
                "цель=сила; уровень=новичок; дни=пн 19:00, ср 19:00; "
                "минуты=45; оборудование=гантели; ограничения=нет",
            )
            plan, workouts, _reminders = await service.generate_week(
                session, 9, self.clock.now()
            )
            await session.commit()

        text = _format_training_plan(plan, workouts, workouts[0])
        self.assertIn(workouts[0].title, text)
        self.assertIn(workouts[1].title, text)
        self.assertIn("Гоблет-присед", text)
        self.assertNotIn("Пуловер с гантелью", text)
        self.assertEqual(1, text.count("Разминка:"))

    async def test_rebalance_moves_only_flexible_remaining_workouts(self):
        service = TrainingService()
        now = self.clock.now()
        async with self.sessions() as session:
            await service.save_profile(
                session,
                10,
                "цель=форма; уровень=регулярно; фиксированные=бокс вт 19:00 тяжёлая; "
                "дни=пт 19:00, пн 19:00, ср 19:00; минуты=45; "
                "оборудование=зал; ограничения=нет",
            )
            _plan, workouts, _reminders = await service.generate_week(session, 10, now)
            target = next(
                item
                for item in workouts
                if not item.is_fixed and item.scheduled_for.weekday() == 4
            )
            fixed = next(item for item in workouts if item.is_fixed)
            fixed_time = fixed.scheduled_for
            new_time = target.scheduled_for + timedelta(days=3)
            moved, moved_reminder, changed, warnings = (
                await service.reschedule_and_rebalance(
                    session,
                    10,
                    target.id,
                    new_time,
                    now,
                )
            )
            await session.commit()

        self.assertEqual(new_time, moved.scheduled_for)
        self.assertEqual(new_time, moved_reminder.remind_at)
        self.assertEqual(fixed_time, fixed.scheduled_for)
        self.assertGreaterEqual(len(changed), 1)
        self.assertEqual([], warnings)
        flexible_times = [
            item.scheduled_for for item in workouts if not item.is_fixed
        ]
        self.assertEqual(len(flexible_times), len(set(flexible_times)))
        self.assertTrue(all(not item[0].is_fixed for item in changed))

    async def test_completed_sets_adapt_the_next_matching_exercise(self):
        service = TrainingService()
        now = self.clock.now()
        async with self.sessions() as session:
            plan = WorkoutPlan(
                user_id=11,
                week_start=now.date(),
                title="Адаптивная неделя",
                plan_json=json.dumps({"safety": "Безопасность"}),
            )
            session.add(plan)
            await session.flush()
            previous = WorkoutSession(
                user_id=11,
                scheduled_for=now - timedelta(days=5),
                title="Прошлая",
                status="completed",
                rpe=7,
                completed_at=now - timedelta(days=5),
            )
            current = WorkoutSession(
                user_id=11,
                plan_id=plan.id,
                scheduled_for=now,
                title="Текущая",
                status="in_progress",
                details_json=json.dumps({"exercises": [{"exercise": "Гоблет-присед"}]}),
            )
            upcoming = WorkoutSession(
                user_id=11,
                plan_id=plan.id,
                scheduled_for=now + timedelta(days=3),
                title="Следующая",
                details_json=json.dumps({
                    "exercises": [{
                        "exercise": "Гоблет-присед",
                        "sets": 3,
                        "reps": "6–10",
                        "target_rpe": "6–8",
                    }]
                }, ensure_ascii=False),
            )
            session.add_all([previous, current, upcoming])
            await session.flush()
            for workout in (previous, current):
                for set_number in range(1, 4):
                    session.add(WorkoutSetLog(
                        user_id=11,
                        workout_session_id=workout.id,
                        exercise_index=0,
                        exercise_name="Гоблет-присед",
                        set_number=set_number,
                        reps=10,
                        weight_kg="20",
                        completed_at=workout.scheduled_for,
                    ))
            await service.complete_workout(session, 11, current.id, now, 7)
            await session.commit()

        details = json.loads(upcoming.details_json)
        exercise = details["exercises"][0]
        self.assertEqual("increase", exercise["progression"])
        self.assertEqual("21", exercise["suggested_weight_kg"])
        self.assertIn("увеличить вес", exercise["progression_note"])

    def test_natural_reschedule_keeps_workout_time_when_only_day_changes(self):
        current = datetime(2026, 7, 31, 20, 15, tzinfo=self.clock.tz)
        target = _workout_target_from_text(
            "перенеси тренировку на завтра",
            current,
            self.clock,
        )
        self.assertEqual(datetime(2026, 8, 1, 20, 15, tzinfo=self.clock.tz), target)
        target = _workout_target_from_text(
            "перенеси тренировку с пятницы на субботу",
            current,
            self.clock,
        )
        self.assertEqual(datetime(2026, 8, 1, 20, 15, tzinfo=self.clock.tz), target)


if __name__ == "__main__":
    unittest.main()
