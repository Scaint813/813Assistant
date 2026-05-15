from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


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
    checkin_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    checkin_count_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    checkin_count_today: Mapped[int] = mapped_column(Integer, default=0)


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_minor: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_cleanup_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
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
    status: Mapped[str] = mapped_column(String(32), default="active")
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    related_entity_type: Mapped[str] = mapped_column(String(64), default="")
    related_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    miro_item_id: Mapped[str] = mapped_column(String(128), default="")
    miro_frame_id: Mapped[str] = mapped_column(String(128), default="")


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


class CleanupLog(Base):
    __tablename__ = "cleanup_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


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
