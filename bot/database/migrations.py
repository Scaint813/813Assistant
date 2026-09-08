from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.database.models import Base

logger = logging.getLogger(__name__)

# Columns to add if missing: (table_name, column_name, column_def)
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("tasks", "category", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "project", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "project_id", "INTEGER"),
    ("tasks", "completed_at", "DATETIME"),
    ("tasks", "outcome", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "next_action", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "importance", "INTEGER NOT NULL DEFAULT 3"),
    ("tasks", "urgency", "INTEGER NOT NULL DEFAULT 3"),
    ("tasks", "estimated_minutes", "INTEGER NOT NULL DEFAULT 30"),
    ("tasks", "duration_confirmed", "BOOLEAN NOT NULL DEFAULT 0"),
    ("tasks", "deadline_confirmed", "BOOLEAN NOT NULL DEFAULT 0"),
    ("tasks", "planning_state", "TEXT NOT NULL DEFAULT 'ready'"),
    ("tasks", "scheduled_start", "DATETIME"),
    ("tasks", "scheduled_end", "DATETIME"),
    ("tasks", "dependencies_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("tasks", "blocked_reason", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "workflow_state", "TEXT NOT NULL DEFAULT 'next'"),
    ("tasks", "priority_reason", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "parent_task_id", "INTEGER"),
    ("reminders", "recurrence", "TEXT NOT NULL DEFAULT 'none'"),
    ("reminders", "timezone", "TEXT NOT NULL DEFAULT 'Europe/Moscow'"),
    ("reminders", "delivery_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("reminders", "last_delivered_at", "DATETIME"),
    ("reminders", "last_delivery_error", "TEXT NOT NULL DEFAULT ''"),
    ("user_runtime_state", "last_screen_chat_id", "INTEGER"),
    ("user_runtime_state", "last_screen_message_id", "INTEGER"),
    ("user_runtime_state", "last_entity_type", "TEXT NOT NULL DEFAULT ''"),
    ("user_runtime_state", "last_entity_id", "INTEGER"),
    ("user_runtime_state", "checkin_count_date", "DATE"),
    ("user_runtime_state", "checkin_count_today", "INTEGER NOT NULL DEFAULT 0"),
    ("health_snapshots", "resting_heart_rate", "INTEGER NOT NULL DEFAULT 0"),
    ("health_snapshots", "hrv_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("training_profiles", "preferred_times_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("training_profiles", "focus_areas", "TEXT NOT NULL DEFAULT 'balanced'"),
    ("training_profiles", "fixed_sessions_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("workout_sessions", "rescheduled_from", "DATETIME"),
    ("workout_sessions", "skip_reason", "TEXT NOT NULL DEFAULT ''"),
    ("workout_sessions", "activity_type", "TEXT NOT NULL DEFAULT 'strength'"),
    ("workout_sessions", "load_level", "TEXT NOT NULL DEFAULT 'medium'"),
    ("workout_sessions", "muscle_groups_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("workout_sessions", "is_fixed", "BOOLEAN NOT NULL DEFAULT 0"),
    ("workout_sessions", "source", "TEXT NOT NULL DEFAULT 'generated'"),
    ("workout_sessions", "started_at", "DATETIME"),
    ("workout_sessions", "current_exercise_index", "INTEGER NOT NULL DEFAULT 0"),
    ("calendar_events", "event_type", "TEXT NOT NULL DEFAULT ''"),
    ("calendar_events", "description", "TEXT NOT NULL DEFAULT ''"),
    ("calendar_events", "location", "TEXT NOT NULL DEFAULT ''"),
    ("calendar_events", "teacher", "TEXT NOT NULL DEFAULT ''"),
    ("calendar_events", "building", "TEXT NOT NULL DEFAULT ''"),
    ("calendar_events", "room", "TEXT NOT NULL DEFAULT ''"),
]


async def create_schema(engine: AsyncEngine) -> None:
    """Create and validate the schema; any required migration failure is fatal."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _run_column_migrations(engine)
    await _validate_schema(engine)


def configure_engine(engine: AsyncEngine) -> None:
    """Apply SQLite reliability settings to every pooled DB connection."""
    if engine.url.get_backend_name() != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


async def _run_column_migrations(engine: AsyncEngine) -> None:
    """Apply legacy column migrations and record every applied version."""
    async with engine.begin() as conn:
        for table, column, col_def in _MIGRATIONS:
            version = f"column:{table}.{column}"
            existing = await _get_columns(conn, table)
            if column not in existing:
                sql = f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"
                await conn.exec_driver_sql(sql)
                logger.info("Migration: added column %s.%s", table, column)
            await conn.exec_driver_sql(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (?, CURRENT_TIMESTAMP)",
                (version,),
            )


async def _get_columns(conn, table: str) -> set[str]:
    """Return set of existing column names for a SQLite table."""
    result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
    rows = result.fetchall()
    return {row[1] for row in rows}


async def _validate_schema(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            expected = {column.name for column in table.columns}
            existing = await _get_columns(conn, table.name)
            missing = expected - existing
            if missing:
                raise RuntimeError(
                    f"Database schema validation failed: {table.name} missing {sorted(missing)}"
                )
        if engine.url.get_backend_name() == "sqlite":
            result = await conn.exec_driver_sql("PRAGMA integrity_check")
            integrity = result.scalar_one()
            if integrity != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {integrity}")
