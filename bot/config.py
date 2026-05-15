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
    allowed_user_id: int
    openai_api_key: str
    openai_model: str
    transcription_model: str
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


def _parse_time(key: str, default: str) -> time:
    raw = os.getenv(key, default)
    hh, mm = raw.split(":")
    return time(hour=int(hh), minute=int(mm))


def _required_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"Missing required env variable: {key}")
    return value


def get_config() -> Config:
    return Config(
        bot_token=_required_env("BOT_TOKEN"),
        bot_id=int(_required_env("BOT_ID")),
        allowed_user_id=int(_required_env("ALLOWED_USER_ID")),


        allowed_user_id=int(os.getenv("ALLOWED_USER_ID", "0")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        transcription_model=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "whisper-1"),
        miro_token=os.getenv("MIRO_ACCESS_TOKEN", ""),
        miro_board_id=os.getenv("MIRO_BOARD_ID", ""),
        miro_ai_zone_start_x=int(os.getenv("MIRO_AI_ZONE_START_X", "5000")),
        miro_ai_zone_start_y=int(os.getenv("MIRO_AI_ZONE_START_Y", "0")),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        morning_time=_parse_time("DEFAULT_MORNING_TIME", "09:00"),
        day_time=_parse_time("DEFAULT_DAY_TIME", "14:00"),
        evening_time=_parse_time("DEFAULT_EVENING_TIME", "19:00"),
        night_time=_parse_time("DEFAULT_NIGHT_TIME", "22:00"),
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./assistant.db"),
    )
