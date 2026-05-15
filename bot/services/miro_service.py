from __future__ import annotations

import logging
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


class MiroService:
    FRAME_ORDER = [
        ("today", "ШТАБ / TODAY", 0),
        ("tasks", "ЗАДАЧИ / TASKS", 3000),
        ("reminders", "НАПОМИНАНИЯ / REMINDERS", 6000),
        ("schedule", "РАСПИСАНИЕ / SCHEDULE", 9000),
        ("money", "ДЕНЬГИ / MONEY", 12000),
        ("orders", "ЗАКАЗЫ / ORDERS", 15000),
        ("study", "УЧЁБА / STUDY", 18000),
        ("body", "ТЕЛО / BODY", 21000),
        ("protocols", "ПРОТОКОЛЫ / PROTOCOLS", 24000),
        ("archive", "АРХИВ / ARCHIVE", 27000),
    ]

    def __init__(self, token: str, board_id: str, start_x: int, start_y: int):
        self.token = token
        self.board_id = board_id
        self.start_x = start_x
        self.start_y = start_y
        self.frame_headers: dict[str, str] = {}

    def is_configured(self) -> bool:
        return bool(self.token and self.board_id)

    async def ensure_frames(self):
        for key, title, offset in self.FRAME_ORDER:
            item_id = self.frame_headers.get(key, "")
            x = self.start_x + offset
            y = self.start_y
            item_id = await self._upsert_sticky(item_id, f"[{title}]", x, y, "light_gray")
            if item_id:
                self.frame_headers[key] = item_id

    async def sync_all(self, tasks, reminders, overrides, archived):
        await self.ensure_frames()
        t = await self.sync_today_frame(tasks, reminders, overrides)
        a = await self.sync_tasks(tasks)
        r = await self.sync_reminders(reminders)
        s = await self.sync_schedule(overrides)
        ar = await self.sync_archive(archived)
        return {"today": t, "tasks": a, "reminders": r, "schedule": s, "archive": ar}

    async def sync_today_frame(self, tasks, reminders, overrides):
        focus = tasks[0].title if tasks else "нет"
        text = f"[ШТАБ]\nДата: {datetime.now().date()}\nФокус: {focus}\nЗадачи: {len(tasks)}\nНапоминания: {len(reminders)}\nРежимы: {len(overrides)}"
        await self._upsert_sticky("", text, self.start_x + 300, self.start_y + 500, "light_yellow")
        return 1

    async def sync_tasks(self, tasks):
        n = 0
        for i, task in enumerate(tasks[:50]):
            if task.status != "active":
                continue
            color = "red" if task.priority in {"high", "urgent"} else ("light_yellow" if task.priority == "medium" else "gray")
            content = f"[ЗАДАЧА]\n{task.title}\nПриоритет: {task.priority}\nСтатус: {task.status}"
            item_id = await self._upsert_sticky(task.miro_item_id, content, self.start_x + 3300, self.start_y + 400 + i * 260, color)
            if item_id:
                task.miro_item_id = item_id
            n += 1
        return n

    async def sync_reminders(self, reminders):
        now = datetime.now(tz=reminders[0].remind_at.tzinfo) if reminders else datetime.now()
        n = 0
        for i, r in enumerate(reminders[:50]):
            if r.status != "active":
                continue
            overdue = "\nПросрочено" if r.remind_at <= now else ""
            color = "red" if r.remind_at <= now else "light_blue"
            content = f"[НАПОМИНАНИЕ]\n{r.text}\nКогда: {r.remind_at}\nСтатус: {r.status}{overdue}"
            item_id = await self._upsert_sticky(r.miro_item_id, content, self.start_x + 6300, self.start_y + 400 + i * 260, color)
            if item_id:
                r.miro_item_id = item_id
            n += 1
        return n

    async def sync_schedule(self, overrides):
        n = 0
        for i, o in enumerate(overrides[:20]):
            content = f"[РАСПИСАНИЕ]\nДата: {o.date}\nРежим: {o.mode}\nОписание: {o.description or '-'}"
            await self._upsert_sticky("", content, self.start_x + 9300, self.start_y + 400 + i * 260, "light_blue")
            n += 1
        return n

    async def sync_archive(self, archived):
        n = 0
        for i, t in enumerate(archived[:20]):
            content = f"[АРХИВ]\n{t.title}\nПричина: {t.cleanup_reason or '-'}\nДата: {t.archived_at or '-'}"
            await self._upsert_sticky(t.miro_item_id, content, self.start_x + 27300, self.start_y + 400 + i * 260, "gray")
            n += 1
        return n

    async def _upsert_sticky(self, item_id: str, content: str, x: int, y: int, color: str) -> str | None:
        if x < self.start_x:
            return None
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
