from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine

from bot.database.models import Base

logger = logging.getLogger(__name__)

# Columns to add if missing: (table_name, column_name, column_def)
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("tasks", "category", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "project", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "completed_at", "DATETIME"),
    ("user_runtime_state", "last_screen_chat_id", "INTEGER"),
    ("user_runtime_state", "last_screen_message_id", "INTEGER"),
]


async def create_schema(engine: AsyncEngine) -> None:
    """Create all tables (safe: skips existing tables)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _run_column_migrations(engine)


async def _run_column_migrations(engine: AsyncEngine) -> None:
    """Add missing columns to existing tables via ALTER TABLE (idempotent)."""
    async with engine.begin() as conn:
        for table, column, col_def in _MIGRATIONS:
            existing = await _get_columns(conn, table)
            if column not in existing:
                sql = f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"
                try:
                    await conn.exec_driver_sql(sql)
                    logger.info("Migration: added column %s.%s", table, column)
                except Exception as exc:
                    logger.warning("Migration skipped %s.%s: %s", table, column, exc)


async def _get_columns(conn, table: str) -> set[str]:
    """Return set of existing column names for a SQLite table."""
    try:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        rows = result.fetchall()
        return {row[1] for row in rows}
    except Exception:
        return set()
