#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import create_async_engine

from bot.database.migrations import configure_engine, create_schema


async def preflight(database: str) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    configure_engine(engine)
    try:
        await create_schema(engine)
    finally:
        await engine.dispose()
    with sqlite3.connect(database) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit(f"Staging migration integrity check failed: {integrity}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: preflight_database.py PATH")
    asyncio.run(preflight(str(Path(sys.argv[1]).resolve())))
