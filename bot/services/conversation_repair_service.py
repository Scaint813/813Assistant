from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from bot.database.models import ConversationRepair
from bot.services.datetime_utils import ensure_aware


class ConversationRepairService:
    """Persist and consume one missing-field clarification without mixing users."""

    TTL = timedelta(minutes=15)

    def __init__(self, time_service):
        self.time_service = time_service

    async def remember(self, session, user_id: int, repair: dict, now: datetime) -> None:
        row = await session.scalar(
            select(ConversationRepair).where(ConversationRepair.user_id == user_id)
        )
        if row is None:
            row = ConversationRepair(
                user_id=user_id,
                kind=str(repair.get("kind") or "unknown")[:32],
                missing_field=str(repair.get("missing_field") or "unknown")[:32],
                original_text=str(repair.get("original_text") or "")[:4000],
                context_json=json.dumps(repair.get("context") or {}, ensure_ascii=False),
                expires_at=now + self.TTL,
            )
            session.add(row)
        else:
            row.kind = str(repair.get("kind") or "unknown")[:32]
            row.missing_field = str(repair.get("missing_field") or "unknown")[:32]
            row.original_text = str(repair.get("original_text") or "")[:4000]
            row.context_json = json.dumps(repair.get("context") or {}, ensure_ascii=False)
            row.expires_at = now + self.TTL
        await session.flush()

    async def merge_followup(
        self, session, user_id: int, text: str, now: datetime
    ) -> tuple[str, bool]:
        row = await session.scalar(
            select(ConversationRepair).where(ConversationRepair.user_id == user_id)
        )
        if row is None:
            return text, False
        expires_at = ensure_aware(row.expires_at, now.tzinfo)
        if not expires_at or expires_at <= now:
            await session.delete(row)
            await session.flush()
            return text, False
        if not self._matches(row.missing_field, text):
            await session.delete(row)
            await session.flush()
            return text, False
        merged = f"{row.original_text.strip()} {text.strip()}".strip()
        await session.delete(row)
        await session.flush()
        return merged, True

    def _matches(self, missing_field: str, text: str) -> bool:
        lowered = text.casefold().strip()
        if lowered in {"отмена", "неважно", "забудь", "ничего"}:
            return False
        if re.match(
            r"^(?:добавь|создай|покажи|перенеси|измени|закрой|заверши|отмени|удали)\b",
            lowered,
        ):
            return False
        if missing_field == "time":
            return bool(
                self.time_service.extract_time_from_text(text)
                or re.search(r"\bчерез\s+\d+\s*(?:мин|час|ч\b|дн)", lowered)
                or any(word in lowered for word in ("утром", "днём", "днем", "вечером", "ночью"))
            )
        if missing_field == "subject":
            return len(lowered) >= 3 and "?" not in lowered
        return False
