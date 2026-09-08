from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import select

from bot.database.models import CalendarEvent, UserProfile
from bot.services.datetime_utils import ensure_aware


@dataclass(frozen=True, slots=True)
class RouteEstimate:
    minutes: int
    approximate: bool
    source: str


@dataclass(frozen=True, slots=True)
class CalendarConflict:
    kind: str
    first: CalendarEvent
    second: CalendarEvent
    available_minutes: int
    required_minutes: int
    missing_minutes: int
    approximate: bool = False


class RouteTimeEstimator:
    """Use saved route facts; fall back to an explicit conservative buffer."""

    def __init__(self, default_transfer_buffer_minutes: int = 45):
        self.default_transfer_buffer_minutes = max(
            0, int(default_transfer_buffer_minutes)
        )

    async def user_context(self, session, user_id: int) -> dict:
        result = await session.execute(
            select(UserProfile.preferences_json).where(UserProfile.user_id == user_id)
        )
        raw = result.scalar_one_or_none() or "{}"
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def estimate(self, origin: str, destination: str, context: dict | None = None) -> RouteEstimate:
        context = context or {}
        origin_key = self._location_key(origin, context)
        destination_key = self._location_key(destination, context)
        if origin_key and origin_key == destination_key:
            return RouteEstimate(0, False, "same_location")

        rules = context.get("rules") if isinstance(context.get("rules"), dict) else {}
        arrival_buffer = self._bounded_minutes(
            rules.get("arrival_buffer_minutes"), default=15
        )
        routes = context.get("route_minutes")
        if isinstance(routes, dict):
            for key in (
                f"{origin_key}->{destination_key}",
                f"{destination_key}<-{origin_key}",
                f"{origin}->{destination}",
            ):
                if key in routes:
                    route_minutes = self._bounded_minutes(routes[key], default=-1)
                    if route_minutes >= 0:
                        return RouteEstimate(
                            route_minutes + arrival_buffer,
                            False,
                            "profile_route",
                        )

        fallback = self._bounded_minutes(
            rules.get("minimum_transfer_buffer_minutes"),
            default=self.default_transfer_buffer_minutes,
        )
        return RouteEstimate(fallback, True, "default_buffer")

    @staticmethod
    def home(context: dict | None) -> str:
        context = context or {}
        home = str(context.get("home") or "").strip()
        if home:
            return home
        locations = context.get("known_locations")
        if isinstance(locations, dict):
            return str(locations.get("HOME") or locations.get("home") or "").strip()
        return ""

    @classmethod
    def _location_key(cls, value: str, context: dict) -> str:
        locations = context.get("known_locations")
        resolved = value
        if isinstance(locations, dict):
            direct = locations.get(value) or locations.get(value.upper()) or locations.get(value.casefold())
            if direct:
                resolved = str(direct)
        return re.sub(r"[^\wа-яё]+", " ", resolved.casefold()).strip()

    @staticmethod
    def _bounded_minutes(value, *, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return min(24 * 60, max(-1, parsed))


class ConflictService:
    def __init__(self, route_estimator: RouteTimeEstimator | None = None):
        self.route_estimator = route_estimator or RouteTimeEstimator()

    async def detect_between(
        self,
        session,
        user_id: int,
        start: datetime,
        end: datetime,
    ) -> list[CalendarConflict]:
        result = await session.execute(
            select(CalendarEvent).where(
                CalendarEvent.user_id == user_id,
                CalendarEvent.is_busy.is_(True),
                CalendarEvent.start_at < end,
                CalendarEvent.end_at > start,
            ).order_by(CalendarEvent.start_at.asc(), CalendarEvent.end_at.asc())
        )
        events = list(result.scalars().all())
        context = await self.route_estimator.user_context(session, user_id)
        return self.detect(events, start.tzinfo, context)

    def detect(
        self,
        events: list[CalendarEvent],
        tz,
        context: dict | None = None,
    ) -> list[CalendarConflict]:
        candidates = sorted(
            events,
            key=lambda item: (
                ensure_aware(item.start_at, tz),
                ensure_aware(item.end_at, tz),
                item.id or 0,
            ),
        )
        ordered: list[CalendarEvent] = []
        for candidate in candidates:
            if any(
                self._same_logical_event(existing, candidate, tz)
                for existing in ordered
            ):
                continue
            ordered.append(candidate)
        conflicts: list[CalendarConflict] = []
        maximum_buffer = 24 * 60
        for index, first in enumerate(ordered):
            first_start = ensure_aware(first.start_at, tz)
            first_end = ensure_aware(first.end_at, tz)
            for second in ordered[index + 1:]:
                second_start = ensure_aware(second.start_at, tz)
                if second_start > first_end + timedelta(minutes=maximum_buffer):
                    break
                second_end = ensure_aware(second.end_at, tz)
                if second_end <= first_start:
                    continue
                available = int((second_start - first_end).total_seconds() // 60)
                if available < 0:
                    overlap_minutes = int(
                        (
                            min(first_end, second_end)
                            - max(first_start, second_start)
                        ).total_seconds()
                        // 60
                    )
                    conflicts.append(CalendarConflict(
                        kind="overlap",
                        first=first,
                        second=second,
                        available_minutes=available,
                        required_minutes=0,
                        missing_minutes=max(1, overlap_minutes),
                    ))
                    continue
                if not first.location or not second.location:
                    continue
                estimate = self.route_estimator.estimate(
                    first.location, second.location, context
                )
                if available < estimate.minutes:
                    conflicts.append(CalendarConflict(
                        kind="transfer",
                        first=first,
                        second=second,
                        available_minutes=available,
                        required_minutes=estimate.minutes,
                        missing_minutes=estimate.minutes - available,
                        approximate=estimate.approximate,
                    ))
                if len(conflicts) >= 20:
                    return conflicts
        return conflicts

    @staticmethod
    def _same_logical_event(first: CalendarEvent, second: CalendarEvent, tz) -> bool:
        """Ignore the same real event mirrored by two calendar sources."""
        if first.source == second.source:
            return False
        return (
            first.title.casefold().strip() == second.title.casefold().strip()
            and (first.location or "").casefold().strip()
            == (second.location or "").casefold().strip()
            and ensure_aware(first.start_at, tz) == ensure_aware(second.start_at, tz)
            and ensure_aware(first.end_at, tz) == ensure_aware(second.end_at, tz)
        )

    @staticmethod
    def render(conflicts: list[CalendarConflict], tz) -> str:
        if not conflicts:
            return "Конфликты\n\nПересечений и коротких переездов не найдено."
        lines = [f"Конфликты · {len(conflicts)}", ""]
        for conflict in conflicts[:10]:
            first_end = ensure_aware(conflict.first.end_at, tz)
            second_start = ensure_aware(conflict.second.start_at, tz)
            if conflict.kind == "overlap":
                lines.append(
                    f"• Пересечение {conflict.missing_minutes} мин: "
                    f"{escape(conflict.first.title)} до {first_end.strftime('%H:%M')} и "
                    f"{escape(conflict.second.title)} с {second_start.strftime('%H:%M')}."
                )
                continue
            estimate_mark = "≈" if conflict.approximate else ""
            lines.append(
                f"• Не хватает {conflict.missing_minutes} мин на переход/дорогу: "
                f"между {escape(conflict.first.title)} и {escape(conflict.second.title)} "
                f"есть {conflict.available_minutes} мин, нужно {estimate_mark}{conflict.required_minutes}."
            )
            lines.append(
                f"  {escape(conflict.first.location)} → {escape(conflict.second.location)}"
            )
        if len(conflicts) > 10:
            lines.append(f"• ещё {len(conflicts) - 10}")
        return "\n".join(lines)
