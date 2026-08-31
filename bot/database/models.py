from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    version: Mapped[str] = mapped_column(String(128), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserProfile(Base, TimestampMixin):
    __tablename__ = "user_profile"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    preferences_json: Mapped[str] = mapped_column(Text, default="{}")


class UserRuntimeState(Base, TimestampMixin):
    __tablename__ = "user_runtime_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    last_user_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checkin_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quiet_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkin_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    checkin_count_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    checkin_count_today: Mapped[int] = mapped_column(Integer, default=0)
    last_screen_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_screen_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_entity_type: Mapped[str] = mapped_column(String(32), default="")
    last_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Project(Base, TimestampMixin):
    """A real project container. Tasks remain the executable units."""

    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(128))
    objective: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    project: Mapped[str] = mapped_column(String(128), default="")
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")
    importance: Mapped[int] = mapped_column(Integer, default=3)
    urgency: Mapped[int] = mapped_column(Integer, default=3)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=30)
    duration_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    deadline_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    planning_state: Mapped[str] = mapped_column(String(16), default="ready")
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dependencies_json: Mapped[str] = mapped_column(Text, default="[]")
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    workflow_state: Mapped[str] = mapped_column(String(16), default="next")
    priority_reason: Mapped[str] = mapped_column(Text, default="")
    parent_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_minor: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_cleanup_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleanup_reason: Mapped[str] = mapped_column(Text, default="")
    miro_item_id: Mapped[str] = mapped_column(String(128), default="")
    miro_frame_id: Mapped[str] = mapped_column(String(128), default="")


class Reminder(Base, TimestampMixin):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    text: Mapped[str] = mapped_column(Text)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    status: Mapped[str] = mapped_column(String(32), default="active")
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    related_entity_type: Mapped[str] = mapped_column(String(64), default="")
    related_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recurrence: Mapped[str] = mapped_column(String(32), default="none")
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_delivery_error: Mapped[str] = mapped_column(Text, default="")
    miro_item_id: Mapped[str] = mapped_column(String(128), default="")
    miro_frame_id: Mapped[str] = mapped_column(String(128), default="")


class ReminderDelivery(Base, TimestampMixin):
    """One durable delivery attempt per scheduled reminder occurrence."""

    __tablename__ = "reminder_deliveries"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_reminder_delivery_occurrence"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    occurrence_key: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


class ScheduleOverride(Base, TimestampMixin):
    __tablename__ = "schedule_overrides"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[date] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(64), default="normal")
    title: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    create_tasks: Mapped[bool] = mapped_column(Boolean, default=True)
    write_to_miro: Mapped[bool] = mapped_column(Boolean, default=False)


class PendingPreview(Base, TimestampMixin):
    __tablename__ = "pending_previews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="text")
    original_text: Mapped[str] = mapped_column(Text, default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    preview_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")


class QualityFeedback(Base, TimestampMixin):
    """A privacy-conscious label linked to a preview; raw text stays in PendingPreview."""

    __tablename__ = "quality_feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    preview_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(16), default="preview")
    verdict: Mapped[str] = mapped_column(String(32), index=True)
    original_hash: Mapped[str] = mapped_column(String(64), default="")
    corrected_hash: Mapped[str] = mapped_column(String(64), default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class ConversationRepair(Base, TimestampMixin):
    """Short-lived missing-field context for a single user's next message."""

    __tablename__ = "conversation_repairs"
    __table_args__ = (UniqueConstraint("user_id", name="uq_conversation_repair_user"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    missing_field: Mapped[str] = mapped_column(String(32))
    original_text: Mapped[str] = mapped_column(Text)
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CleanupLog(Base):
    __tablename__ = "cleanup_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProblemBlock(Base, TimestampMixin):
    __tablename__ = "problem_blocks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    category: Mapped[str] = mapped_column(String(32), default="other")
    title: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    problem_text: Mapped[str] = mapped_column(Text, default="")
    solution_strategy: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    pressure_level: Mapped[str] = mapped_column(String(16), default="normal")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default="text")
    resources_json: Mapped[str] = mapped_column(Text, default="[]")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archive_reason: Mapped[str] = mapped_column(Text, default="")


class ProblemBlockEvent(Base):
    __tablename__ = "problem_block_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    problem_block_id: Mapped[int] = mapped_column(Integer, index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MiroMapping(Base, TimestampMixin):
    __tablename__ = "miro_mappings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    board_id: Mapped[str] = mapped_column(String(128), default="")
    frame_id: Mapped[str] = mapped_column(String(128), default="")
    item_id: Mapped[str] = mapped_column(String(128), default="")
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)


class ExamDate(Base, TimestampMixin):
    """Stores EGE/exam dates per subject. NOT stored as reminders."""
    __tablename__ = "exam_dates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    subject: Mapped[str] = mapped_column(String(128))       # Обществознание, Русский...
    exam_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | passed | cancelled
    notes: Mapped[str] = mapped_column(Text, default="")


class StudyScheduleItem(Base, TimestampMixin):
    """Stores tutor sessions and recurring study slots. NOT stored as reminders."""
    __tablename__ = "study_schedule_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    subject: Mapped[str] = mapped_column(String(128))        # Английский, Общество...
    title: Mapped[str] = mapped_column(String(255), default="")
    weekday: Mapped[str] = mapped_column(String(16), default="")   # mon/tue/wed/thu/fri/sat/sun
    time_str: Mapped[str] = mapped_column(String(16), default="")  # "18:00"
    recurrence: Mapped[str] = mapped_column(String(32), default="weekly")  # weekly/once
    tutor_name: Mapped[str] = mapped_column(String(128), default="")
    location_or_link: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | paused | done
    notes: Mapped[str] = mapped_column(Text, default="")


class ActionLog(Base, TimestampMixin):
    __tablename__ = "action_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    batch_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    action_type: Mapped[str] = mapped_column(String(32), default="create")
    entity_type: Mapped[str] = mapped_column(String(64), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    source: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="applied")
    undoable: Mapped[bool] = mapped_column(Boolean, default=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProductMetric(Base):
    __tablename__ = "product_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class CalendarEvent(Base, TimestampMixin):
    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint("user_id", "external_id", name="uq_calendar_event_user_external"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    calendar_name: Mapped[str] = mapped_column(String(128), default="Основной")
    title: Mapped[str] = mapped_column(String(255), default="Занято")
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_busy: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(32), default="shortcut")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TaskPlanBlock(Base, TimestampMixin):
    """A bounded work block; the parent task keeps the full effort estimate."""

    __tablename__ = "task_plan_blocks"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "task_id", "start_at", name="uq_task_plan_block_slot"
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    plan_date: Mapped[date] = mapped_column(Date, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    planned_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="planned")
    source: Mapped[str] = mapped_column(String(32), default="assistant_overflow")


class FocusSession(Base, TimestampMixin):
    __tablename__ = "focus_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    planned_minutes: Mapped[int] = mapped_column(Integer, default=25)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class MemoryEntry(Base, TimestampMixin):
    __tablename__ = "memory_entries"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_memory_user_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="explicit")


class HealthSnapshot(Base, TimestampMixin):
    __tablename__ = "health_snapshots"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_health_snapshot_user_date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    step_goal: Mapped[int] = mapped_column(Integer, default=10000)
    active_energy_kcal: Mapped[int] = mapped_column(Integer, default=0)
    workout_minutes: Mapped[int] = mapped_column(Integer, default=0)
    sleep_minutes: Mapped[int] = mapped_column(Integer, default=0)
    resting_heart_rate: Mapped[int] = mapped_column(Integer, default=0)
    hrv_ms: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(32), default="shortcut")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class HealthNudge(Base, TimestampMixin):
    __tablename__ = "health_nudges"
    __table_args__ = (UniqueConstraint("user_id", "date", "kind", name="uq_health_nudge_user_date_kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="steps")
    status: Mapped[str] = mapped_column(String(16), default="sent")
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TrainingProfile(Base, TimestampMixin):
    __tablename__ = "training_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    experience: Mapped[str] = mapped_column(String(32), default="beginner")
    equipment: Mapped[str] = mapped_column(Text, default="bodyweight")
    limitations: Mapped[str] = mapped_column(Text, default="")
    days_per_week: Mapped[int] = mapped_column(Integer, default=3)
    session_minutes: Mapped[int] = mapped_column(Integer, default=45)
    preferred_days_json: Mapped[str] = mapped_column(Text, default="[]")
    preferred_times_json: Mapped[str] = mapped_column(Text, default="{}")
    focus_areas: Mapped[str] = mapped_column(Text, default="balanced")
    fixed_sessions_json: Mapped[str] = mapped_column(Text, default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WorkoutPlan(Base, TimestampMixin):
    __tablename__ = "workout_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    adjustment_reason: Mapped[str] = mapped_column(Text, default="")


class WorkoutSession(Base, TimestampMixin):
    __tablename__ = "workout_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    title: Mapped[str] = mapped_column(String(255), default="Тренировка")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="planned")
    rpe: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rescheduled_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    skip_reason: Mapped[str] = mapped_column(Text, default="")
    activity_type: Mapped[str] = mapped_column(String(32), default="strength")
    load_level: Mapped[str] = mapped_column(String(16), default="medium")
    muscle_groups_json: Mapped[str] = mapped_column(Text, default="[]")
    is_fixed: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="generated")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_exercise_index: Mapped[int] = mapped_column(Integer, default=0)


class WorkoutSetLog(Base, TimestampMixin):
    __tablename__ = "workout_set_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    workout_session_id: Mapped[int] = mapped_column(Integer, index=True)
    exercise_index: Mapped[int] = mapped_column(Integer, default=0)
    exercise_name: Mapped[str] = mapped_column(String(255))
    set_number: Mapped[int] = mapped_column(Integer)
    reps: Mapped[int] = mapped_column(Integer)
    weight_kg: Mapped[str] = mapped_column(String(32), default="")
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
