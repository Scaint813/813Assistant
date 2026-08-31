from __future__ import annotations

import re
from difflib import SequenceMatcher

from sqlalchemy import and_, select

from bot.database.models import Reminder, Task, UserRuntimeState


class EntityResolver:
    """Resolve a natural reference without ever crossing the user's data boundary."""

    async def resolve(self, session, user_id: int, intent: dict) -> tuple[dict, str | None]:
        intent_type = str(intent.get("type") or "")
        if intent_type in {"update_task", "complete_task", "archive_task"}:
            return await self._resolve_task(session, user_id, intent)
        if intent_type in {"update_reminder", "cancel_reminder"}:
            return await self._resolve_reminder(session, user_id, intent)
        return intent, None

    async def _resolve_task(self, session, user_id: int, intent: dict) -> tuple[dict, str | None]:
        result = await session.execute(
            select(Task).where(
                and_(Task.user_id == user_id, Task.status == "active")
            ).order_by(Task.updated_at.desc(), Task.id.desc())
        )
        tasks = list(result.scalars().all())
        target = str(intent.get("target_title") or intent.get("target") or "").strip()
        entity = await self._pick(session, user_id, "task", tasks, target, intent.get("target_id"))
        if isinstance(entity, list):
            labels = "\n".join(f"• {item.title}" for item in entity[:3])
            return intent, f"Какую задачу ты имеешь в виду?\n\n{labels}\n\nНапиши её название точнее."
        if entity is None:
            return intent, "Не нашёл такую активную задачу. Напиши её название точнее или попроси показать все дела."
        resolved = dict(intent)
        resolved.update({
            "entity_id": entity.id,
            "current_title": entity.title,
            "current_deadline": entity.deadline.isoformat() if entity.deadline else None,
            "current_project": entity.project,
        })
        return resolved, None

    async def _resolve_reminder(self, session, user_id: int, intent: dict) -> tuple[dict, str | None]:
        result = await session.execute(
            select(Reminder).where(
                and_(Reminder.user_id == user_id, Reminder.status.in_(["active", "paused"]))
            ).order_by(Reminder.remind_at.asc(), Reminder.id.desc())
        )
        reminders = list(result.scalars().all())
        target = str(intent.get("target_text") or intent.get("target") or "").strip()
        entity = await self._pick(session, user_id, "reminder", reminders, target, intent.get("target_id"))
        if isinstance(entity, list):
            labels = "\n".join(f"• {item.text}" for item in entity[:3])
            return intent, f"Какое напоминание изменить?\n\n{labels}\n\nНапиши его смысл точнее."
        if entity is None:
            return intent, "Не нашёл такое активное напоминание. Напиши его смысл точнее или попроси показать напоминания."
        resolved = dict(intent)
        resolved.update({
            "entity_id": entity.id,
            "current_text": entity.text,
            "current_remind_at": entity.remind_at.isoformat(),
            "current_recurrence": entity.recurrence,
        })
        return resolved, None

    async def _pick(self, session, user_id: int, entity_type: str, items: list, target: str, target_id):
        if target_id:
            try:
                wanted = int(target_id)
            except (TypeError, ValueError):
                wanted = 0
            exact = next((item for item in items if item.id == wanted), None)
            if exact:
                return exact

        normalized = self._normalize(target)
        pronouns = {"", "это", "эту", "его", "её", "последнее", "последнюю", "текущее", "текущую"}
        if normalized in pronouns:
            state_result = await session.execute(
                select(UserRuntimeState).where(UserRuntimeState.user_id == user_id)
            )
            state = state_result.scalar_one_or_none()
            if state and state.last_entity_type == entity_type and state.last_entity_id:
                recent = next((item for item in items if item.id == state.last_entity_id), None)
                if recent:
                    return recent
            return items[0] if len(items) == 1 else (items[:3] if items else None)

        scored = []
        for item in items:
            label = item.title if entity_type == "task" else item.text
            score = self._score(normalized, self._normalize(label))
            if score >= 0.38:
                scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
        if not scored:
            return None
        if len(scored) > 1 and scored[0][0] < 0.92 and scored[0][0] - scored[1][0] < 0.12:
            return [pair[1] for pair in scored[:3]]
        return scored[0][1]

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(re.findall(r"[a-zа-яё0-9]+", value.casefold()))

    @classmethod
    def _score(cls, target: str, label: str) -> float:
        if target == label:
            return 1.0
        if target and (target in label or label in target):
            return 0.9
        target_words = set(target.split())
        label_words = set(label.split())
        overlap = len(target_words & label_words) / max(1, len(target_words | label_words))
        sequence = SequenceMatcher(None, target, label).ratio()
        return max(overlap, sequence * 0.8)
