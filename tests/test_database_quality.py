from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.migrations import configure_engine, create_schema
from bot.database.models import SchemaMigration


class DatabaseQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_is_versioned_validated_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.sqlite3"
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            configure_engine(engine)
            try:
                await create_schema(engine)
                await create_schema(engine)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                async with sessions() as session:
                    versions = await session.scalar(select(func.count(SchemaMigration.version)))
                    journal_mode = await session.scalar(select(func.sqlite_version()))
                    foreign_keys = (
                        await session.connection()
                    )
                    pragma = await foreign_keys.exec_driver_sql("PRAGMA foreign_keys")
                    integrity = await foreign_keys.exec_driver_sql("PRAGMA integrity_check")
                    foreign_keys_value = pragma.scalar_one()
                    integrity_value = integrity.scalar_one()
                self.assertGreater(versions, 0)
                self.assertTrue(journal_mode)
                self.assertEqual(1, foreign_keys_value)
                self.assertEqual("ok", integrity_value)
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
