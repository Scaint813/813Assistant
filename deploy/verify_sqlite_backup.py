#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path


def verify(source: Path) -> None:
    if not source.is_file():
        raise SystemExit(f"Database backup is missing: {source}")
    with tempfile.TemporaryDirectory(prefix="813assistant-deploy-restore-") as directory:
        restored = Path(directory) / "restored.sqlite3"
        source_uri = f"file:{source.resolve()}?mode=ro"
        with (
            sqlite3.connect(source_uri, uri=True) as source_db,
            sqlite3.connect(restored) as restored_db,
        ):
            source_db.backup(restored_db)
            integrity = restored_db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                row[0]
                for row in restored_db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        if integrity != "ok":
            raise SystemExit(f"Backup integrity check failed: {integrity}")
        required = {
            "schema_migrations",
            "user_profile",
            "user_runtime_state",
            "tasks",
            "reminders",
        }
        if not required.issubset(tables):
            missing = ", ".join(sorted(required - tables))
            raise SystemExit(f"Backup restore is missing tables: {missing}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_sqlite_backup.py PATH")
    verify(Path(sys.argv[1]))
