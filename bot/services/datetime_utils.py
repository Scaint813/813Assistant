"""
Datetime utility helpers for 813Assistant.

SQLite always returns timezone-naive datetimes even when the column has timezone=True.
Use these helpers before ANY datetime comparison to avoid:
    TypeError: can't compare offset-naive and offset-aware datetimes
"""
from __future__ import annotations

from datetime import UTC, datetime


def ensure_aware(dt: datetime | None, tz=None) -> datetime | None:
    """
    Make a datetime timezone-aware.

    - If dt is None → return None
    - If dt.tzinfo is None (naive) → attach tz (defaults to UTC)
    - If dt.tzinfo is already set → convert to tz

    Usage:
        now = time_service.now()  # always aware
        last_activity = ensure_aware(state.last_user_activity_at, now.tzinfo)
        if last_activity and last_activity >= now - timedelta(minutes=45):
            ...
    """
    if dt is None:
        return None
    if tz is None:
        tz = UTC
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def ensure_utc_aware(dt: datetime | None) -> datetime | None:
    """Convenience wrapper: make dt UTC-aware. Returns None if dt is None."""
    return ensure_aware(dt, UTC)


def safe_lt(a: datetime | None, b: datetime | None, tz=None) -> bool:
    """Return a < b safely (False if either is None)."""
    if a is None or b is None:
        return False
    return ensure_aware(a, tz) < ensure_aware(b, tz)


def safe_lte(a: datetime | None, b: datetime | None, tz=None) -> bool:
    """Return a <= b safely (False if either is None)."""
    if a is None or b is None:
        return False
    return ensure_aware(a, tz) <= ensure_aware(b, tz)


def safe_gt(a: datetime | None, b: datetime | None, tz=None) -> bool:
    """Return a > b safely (False if either is None)."""
    if a is None or b is None:
        return False
    return ensure_aware(a, tz) > ensure_aware(b, tz)


def safe_gte(a: datetime | None, b: datetime | None, tz=None) -> bool:
    """Return a >= b safely (False if either is None)."""
    if a is None or b is None:
        return False
    return ensure_aware(a, tz) >= ensure_aware(b, tz)
