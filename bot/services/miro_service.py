from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class MiroService:
    def __init__(self, token: str, board_id: str, start_x: int, start_y: int):
        self.token = token
        self.board_id = board_id
        self.start_x = start_x
        self.start_y = start_y

    def is_configured(self) -> bool:
        return bool(self.token and self.board_id)

    async def create_or_update_task(self, task, index: int) -> str | None:
        if not self.is_configured():
            return None
        x = self.start_x + 500
        y = self.start_y + 300 * index
        return await self._upsert_sticky(task.miro_item_id, f"[TASK] {task.title}\npriority={task.priority}\nstatus={task.status}", x, y, "light_yellow")

    async def create_or_update_reminder(self, reminder, index: int) -> str | None:
        if not self.is_configured():
            return None
        x = self.start_x + 2200
        y = self.start_y + 300 * index
        return await self._upsert_sticky(reminder.miro_item_id, f"[REMINDER] {reminder.text}\n{reminder.remind_at}", x, y, "light_blue")

    async def _upsert_sticky(self, item_id: str, content: str, x: int, y: int, color: str) -> str | None:
        headers = {"Authorization": f"Bearer {self.token}"}
        body = {"data": {"content": content}, "position": {"x": x, "y": y}, "style": {"fillColor": color}}
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                if item_id:
                    r = await client.patch(f"https://api.miro.com/v2/boards/{self.board_id}/sticky_notes/{item_id}", headers=headers, json=body)
                    if r.status_code < 400:
                        return item_id
                r = await client.post(f"https://api.miro.com/v2/boards/{self.board_id}/sticky_notes", headers=headers, json=body)
                r.raise_for_status()
                return r.json().get("id")
            except Exception as exc:
                logger.exception("Miro sync failed: %s", exc)
                return None
