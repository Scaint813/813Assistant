from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.database.queries import get_or_create_user_profile

TIMEZONE_PRESETS: tuple[tuple[str, str], ...] = (
    ("Europe/Moscow", "Москва"),
    ("Asia/Almaty", "Алматы"),
    ("Asia/Yerevan", "Армения · Ереван"),
    ("Europe/Nicosia", "Кипр · Никосия"),
    ("Europe/Istanbul", "Стамбул"),
    ("Asia/Tbilisi", "Тбилиси"),
    ("UTC", "UTC"),
)


class UserProfileService:
    """Resolve per-user clocks and validate profile settings."""

    def __init__(self, default_timezone: str | ZoneInfo):
        self.default_timezone = str(default_timezone)

    async def get(self, session, user_id: int, name: str = ""):
        return await get_or_create_user_profile(
            session, user_id, name, self.default_timezone
        )

    async def time_service_for(self, session, user_id: int, base_time_service):
        profile = await self.get(session, user_id)
        return base_time_service.in_timezone(self.valid_timezone(profile.timezone))

    async def now_for(self, session, user_id: int, base_time_service) -> datetime:
        return (await self.time_service_for(session, user_id, base_time_service)).now()

    async def set_timezone(self, session, user_id: int, timezone: str):
        profile = await self.get(session, user_id)
        profile.timezone = self.valid_timezone(timezone)
        await session.flush()
        return profile

    def valid_timezone(self, timezone: str | None) -> str:
        candidate = str(timezone or self.default_timezone)
        try:
            ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            return self.default_timezone
        return candidate

    def options(self, at: datetime | None = None) -> list[dict[str, str]]:
        at = at or datetime.now(tz=ZoneInfo("UTC"))
        return [
            {
                "timezone": timezone,
                "label": label,
                "offset": self.offset_label(timezone, at),
            }
            for timezone, label in TIMEZONE_PRESETS
        ]

    @staticmethod
    def offset_label(timezone: str, at: datetime | None = None) -> str:
        tz = ZoneInfo(timezone)
        at = (at or datetime.now(tz=tz)).astimezone(tz)
        offset = at.strftime("%z")
        return f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"

    @staticmethod
    def city_label(timezone: str) -> str:
        return dict(TIMEZONE_PRESETS).get(timezone, timezone.replace("_", " "))

    def display_timezone(self, timezone: str, at: datetime | None = None) -> str:
        valid = self.valid_timezone(timezone)
        return (
            f"{self.city_label(valid)} · {self.offset_label(valid, at)}"
            f"\n{valid}"
        )
