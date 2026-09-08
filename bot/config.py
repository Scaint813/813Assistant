from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass(slots=True)
class Config:
    bot_token: str
    bot_id: int
    # Primary owner/admin. Kept as a separate value so operational commands
    # cannot accidentally become available when the allowlist is expanded.
    allowed_user_id: int
    allowed_user_ids: tuple[int, ...]
    max_allowed_users: int
    openai_api_key: str
    openai_model: str
    openai_model_fast: str
    openai_model_smart: str
    openai_reasoning_fast: str
    openai_reasoning_smart: str
    openai_max_output_fast: int
    openai_max_output_smart: int
    transcription_model: str
    miro_sync_enabled: bool
    miro_token: str
    miro_board_id: str
    miro_ai_zone_start_x: int
    miro_ai_zone_start_y: int
    timezone: ZoneInfo
    morning_time: time
    day_time: time
    evening_time: time
    night_time: time
    database_url: str
    checkin_enabled: bool
    checkin_morning_time: time
    checkin_day_time: time
    checkin_evening_time: time
    health_bridge_enabled: bool
    health_bridge_host: str
    health_bridge_port: int
    health_bridge_token: str
    health_nudge_time: time
    default_step_goal: int
    health_min_step_gap: int
    hse_ical_sync_enabled: bool
    hse_ical_url: str
    hse_ical_sync_interval_minutes: int
    default_transfer_buffer_minutes: int
    reliability_enabled: bool
    backup_dir: str
    backup_retention_days: int


def _parse_time(key: str, default: str) -> time:
    raw = os.getenv(key, default)
    hh, mm = raw.split(":")
    return time(hour=int(hh), minute=int(mm))


def _required_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"Missing required env variable: {key}")
    return value


def _parse_allowed_user_ids(primary_user_id: int, raw: str, limit: int = 5) -> tuple[int, ...]:
    """Build a deterministic, closed allowlist with the owner first."""
    if not 1 <= limit <= 5:
        raise ValueError("MAX_ALLOWED_USERS must be between 1 and 5")

    values = [primary_user_id]
    normalized = raw.replace(";", ",").replace(" ", ",")
    for part in normalized.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            user_id = int(part)
        except ValueError as exc:
            raise ValueError(f"Invalid Telegram user id in ALLOWED_USER_IDS: {part}") from exc
        if user_id <= 0:
            raise ValueError("Telegram user ids in ALLOWED_USER_IDS must be positive")
        if user_id not in values:
            values.append(user_id)

    if len(values) > limit:
        raise ValueError(
            f"ALLOWED_USER_IDS contains {len(values)} users; configured limit is {limit}"
        )
    return tuple(values)


def get_config() -> Config:
    primary_user_id = int(_required_env("ALLOWED_USER_ID"))
    max_allowed_users = int(os.getenv("MAX_ALLOWED_USERS", "5"))
    allowed_user_ids = _parse_allowed_user_ids(
        primary_user_id,
        os.getenv("ALLOWED_USER_IDS", ""),
        max_allowed_users,
    )
    return Config(
        bot_token=_required_env("BOT_TOKEN"),
        bot_id=int(_required_env("BOT_ID")),
        allowed_user_id=primary_user_id,
        allowed_user_ids=allowed_user_ids,
        max_allowed_users=max_allowed_users,
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        openai_model_fast=os.getenv("OPENAI_MODEL_FAST", "gpt-5.6-luna")
        or os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        openai_model_smart=os.getenv("OPENAI_MODEL_SMART", "gpt-5.6-terra")
        or os.getenv("OPENAI_MODEL_FAST", "gpt-5.6-luna")
        or os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        openai_reasoning_fast=os.getenv("OPENAI_REASONING_FAST", "low"),
        openai_reasoning_smart=os.getenv("OPENAI_REASONING_SMART", "medium"),
        openai_max_output_fast=int(os.getenv("OPENAI_MAX_OUTPUT_FAST", "1800")),
        openai_max_output_smart=int(os.getenv("OPENAI_MAX_OUTPUT_SMART", "5000")),
        transcription_model=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "whisper-1"),
        miro_sync_enabled=os.getenv("MIRO_SYNC_ENABLED", "false").lower() == "true",
        miro_token=os.getenv("MIRO_ACCESS_TOKEN", ""),
        miro_board_id=os.getenv("MIRO_BOARD_ID", ""),
        miro_ai_zone_start_x=int(os.getenv("MIRO_AI_ZONE_START_X", "12000")),
        miro_ai_zone_start_y=int(os.getenv("MIRO_AI_ZONE_START_Y", "3000")),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        morning_time=_parse_time("DEFAULT_MORNING_TIME", "09:00"),
        day_time=_parse_time("DEFAULT_DAY_TIME", "14:00"),
        evening_time=_parse_time("DEFAULT_EVENING_TIME", "19:00"),
        night_time=_parse_time("DEFAULT_NIGHT_TIME", "22:00"),
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./assistant.db"),
        checkin_enabled=os.getenv("CHECKIN_ENABLED", "false").lower() == "true",
        checkin_morning_time=_parse_time("CHECKIN_MORNING_TIME", "09:30"),
        checkin_day_time=_parse_time("CHECKIN_DAY_TIME", "14:30"),
        checkin_evening_time=_parse_time("CHECKIN_EVENING_TIME", "21:30"),
        health_bridge_enabled=os.getenv("HEALTH_BRIDGE_ENABLED", "false").lower() == "true",
        health_bridge_host=os.getenv("HEALTH_BRIDGE_HOST", "127.0.0.1"),
        health_bridge_port=int(os.getenv("HEALTH_BRIDGE_PORT", "8781")),
        health_bridge_token=os.getenv("HEALTH_BRIDGE_TOKEN", ""),
        health_nudge_time=_parse_time("HEALTH_NUDGE_TIME", "18:00"),
        default_step_goal=int(os.getenv("DEFAULT_STEP_GOAL", "10000")),
        health_min_step_gap=int(os.getenv("HEALTH_MIN_STEP_GAP", "1000")),
        hse_ical_sync_enabled=os.getenv("HSE_ICAL_SYNC_ENABLED", "false").lower()
        == "true",
        hse_ical_url=os.getenv("HSE_ICAL_URL", ""),
        hse_ical_sync_interval_minutes=int(
            os.getenv("HSE_ICAL_SYNC_INTERVAL_MINUTES", "60")
        ),
        default_transfer_buffer_minutes=int(
            os.getenv("DEFAULT_TRANSFER_BUFFER_MINUTES", "45")
        ),
        reliability_enabled=os.getenv("RELIABILITY_ENABLED", "true").lower() == "true",
        backup_dir=os.getenv("BACKUP_DIR", "./backups"),
        backup_retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "14")),
    )
