from __future__ import annotations

import logging
from datetime import datetime

import httpx

from bot.database.queries import (
    get_active_problem_blocks,
    get_active_reminders,
    get_active_tasks,
    get_archived_tasks,
    get_or_create_miro_mapping,
    get_or_create_runtime_state,
    get_upcoming_overrides,
)

logger = logging.getLogger(__name__)


class MiroService:
    FRAME_ORDER = [
        ("today_summary", "ШТАБ / TODAY", 0),
        ("tasks", "ЗАДАЧИ / TASKS", 3000),
        ("reminders", "НАПОМИНАНИЯ / REMINDERS", 6000),
        ("problem_blocks", "ПРОБЛЕМЫ / PROBLEM BLOCKS", 9000),
        ("schedule", "РАСПИСАНИЕ / SCHEDULE", 12000),
        ("checkin_summary", "CHECK-INS", 15000),
        ("money", "ДЕНЬГИ / MONEY", 18000),
        ("orders", "ЗАКАЗЫ / ORDERS", 21000),
        ("study", "УЧЁБА / STUDY", 24000),
        ("body", "ТЕЛО / BODY", 27000),
        ("protocols", "ПРОТОКОЛЫ / PROTOCOLS", 30000),
        ("archive", "АРХИВ / ARCHIVE", 33000),
    ]

    def __init__(self, token: str, board_id: str, start_x: int, start_y: int):
        self.token = token
        self.board_id = board_id
        self.start_x = start_x
        self.start_y = start_y
        self.frame_headers: dict[str, str] = {}

    def is_configured(self) -> bool:
        return bool(self.token and self.board_id)

    async def sync_all(self, user_id: int, session, time_service, cfg) -> dict:
        frames = await self.ensure_frames(user_id, session)
        today = await self.sync_today_frame(user_id, session, time_service, frames)
        tasks = await self.sync_tasks(user_id, session, time_service, frames)
        reminders = await self.sync_reminders(user_id, session, time_service, frames)
        problems = await self.sync_problem_blocks(user_id, session, time_service, frames)
        schedule = await self.sync_schedule(user_id, session, time_service, frames)
        checkins = await self.sync_checkins(user_id, session, time_service, cfg, frames)
        archive = await self.sync_archive(user_id, session, time_service, frames)
        return {"today": today, "tasks": tasks, "reminders": reminders, "problems": problems, "schedule": schedule, "checkins": checkins, "archive": archive}

    async def ensure_frames(self, user_id: int, session):
        out = {}
        for key, title, offset in self.FRAME_ORDER:
            x = self.start_x + offset
            y = self.start_y
            mp = await get_or_create_miro_mapping(session, user_id, "frame_header", offset, self.board_id)
            item_id = await self.create_or_update_item(mp.item_id, f"[{title}]", x, y, "light_gray")
            if item_id:
                mp.item_id = item_id
                mp.x = x
                mp.y = y
            out[key] = {"x": x, "y": y, "id": item_id}
        return out

    async def sync_today_frame(self, user_id: int, session, time_service, frames):
        now = time_service.now()
        tasks = await get_active_tasks(session, user_id)
        reminders = await get_active_reminders(session, user_id)
        blocks = await get_active_problem_blocks(session, user_id, now)
        overrides = await get_upcoming_overrides(session, user_id, now.date())
        state = await get_or_create_runtime_state(session, user_id)
        mode = "отдых" if any(str(o.date) == str(now.date()) and o.mode == "rest_day" for o in overrides) else "обычный"
        if state.quiet_until and state.quiet_until > now:
            mode = "тихий режим"
        risks = []
        if any(t.deadline and t.deadline < now for t in tasks):
            risks.append("просрочка")
        if not state.checkin_enabled:
            risks.append("check-ins disabled")
        text = (
            f"[ШТАБ]\nДата: {now.date()}\nРежим: {mode}\n"
            f"Фокус: {(tasks[0].title if tasks else 'нет')}\n"
            f"Задачи: {len(tasks)}\nНапоминания: {len(reminders)}\n"
            f"Проблемные блоки: {min(len(blocks),2)}\n"
            f"Риски: {', '.join(risks) if risks else 'нет'}"
        )
        mp = await get_or_create_miro_mapping(session, user_id, "today_summary", 0, self.board_id)
        item_id = await self.create_or_update_item(mp.item_id, text, frames["today_summary"]["x"] + 200, frames["today_summary"]["y"] + 450, "light_yellow")
        if item_id:
            mp.item_id = item_id
        return 1

    async def sync_tasks(self, user_id: int, session, time_service, frames):
        tasks = await get_active_tasks(session, user_id)
        total = len(tasks)
        for i, task in enumerate(tasks[:15]):
            color = "red" if task.priority in {"urgent", "high"} else ("yellow" if task.priority == "medium" else "light_gray")
            dl = task.deadline.isoformat() if task.deadline else "-"
            content = f"[ЗАДАЧА]\n{task.title}\nПриоритет: {task.priority}\nДедлайн: {dl}\nСтатус: {task.status}"
            item_id = await self.create_or_update_item(task.miro_item_id, content, frames["tasks"]["x"] + 250, frames["tasks"]["y"] + 380 + i * 240, color)
            if item_id:
                task.miro_item_id = item_id
        if total > 15:
            await self.create_or_update_item("", f"+ ещё {total-15} задач в базе", frames["tasks"]["x"] + 250, frames["tasks"]["y"] + 380 + 15 * 240, "light_gray")
        return min(total, 15)

    async def sync_reminders(self, user_id: int, session, time_service, frames):
        reminders = await get_active_reminders(session, user_id)
        now = time_service.now()
        reminders = sorted(reminders, key=lambda r: r.remind_at)[:10]
        for i, r in enumerate(reminders):
            overdue = r.remind_at <= now
            color = "red" if overdue else "light_blue"
            content = f"[НАПОМИНАНИЕ]\n{r.text}\nКогда: {r.remind_at}\nСтатус: active" + ("\nПросрочено" if overdue else "")
            item_id = await self.create_or_update_item(r.miro_item_id, content, frames["reminders"]["x"] + 250, frames["reminders"]["y"] + 380 + i * 240, color)
            if item_id:
                r.miro_item_id = item_id
        return len(reminders)

    async def sync_problem_blocks(self, user_id: int, session, time_service, frames):
        blocks = await get_active_problem_blocks(session, user_id, time_service.now())
        for i, b in enumerate(blocks[:10]):
            color = "red" if (b.priority in {"urgent", "high"} or b.pressure_level == "hard") else ("yellow" if b.priority == "medium" else "light_blue")
            content = f"[БЛОК]\n{b.title}\nКатегория: {b.category}\nДавление: {b.pressure_level}\nСледующий шаг: {b.next_action}\nДедлайн: {b.deadline or '-'}"
            mp = await get_or_create_miro_mapping(session, user_id, "problem_block", b.id, self.board_id)
            item_id = await self.create_or_update_item(mp.item_id, content, frames["problem_blocks"]["x"] + 250, frames["problem_blocks"]["y"] + 380 + i * 260, color)
            if item_id:
                mp.item_id = item_id
        return min(len(blocks), 10)

    async def sync_schedule(self, user_id: int, session, time_service, frames):
        overrides = await get_upcoming_overrides(session, user_id, time_service.today())
        for i, o in enumerate(overrides[:10]):
            color = "light_green" if o.mode == "rest_day" else "light_blue"
            content = f"[РАСПИСАНИЕ]\nДата: {o.date}\nРежим: {o.mode}\nОписание: {o.description or '-'}"
            mp = await get_or_create_miro_mapping(session, user_id, "schedule_override", o.id, self.board_id)
            item_id = await self.create_or_update_item(mp.item_id, content, frames["schedule"]["x"] + 250, frames["schedule"]["y"] + 380 + i * 240, color)
            if item_id:
                mp.item_id = item_id
        return min(len(overrides), 10)

    async def sync_checkins(self, user_id: int, session, time_service, cfg, frames):
        state = await get_or_create_runtime_state(session, user_id)
        quiet = state.quiet_until.isoformat() if state.quiet_until else "нет"
        text = (
            "[CHECK-INS]\n"
            f"Статус: {'enabled' if state.checkin_enabled else 'disabled'}\n"
            f"Последняя проверка: {state.last_checkin_at or '-'}\n"
            f"Тихий режим: {quiet}\n"
            f"План: {cfg.checkin_morning_time.strftime('%H:%M')} / {cfg.checkin_day_time.strftime('%H:%M')} / {cfg.checkin_evening_time.strftime('%H:%M')}"
        )
        color = "gray" if not state.checkin_enabled else ("light_yellow" if state.quiet_until and state.quiet_until > time_service.now() else "light_green")
        mp = await get_or_create_miro_mapping(session, user_id, "checkin_summary", 0, self.board_id)
        item_id = await self.create_or_update_item(mp.item_id, text, frames["checkin_summary"]["x"] + 250, frames["checkin_summary"]["y"] + 380, color)
        if item_id:
            mp.item_id = item_id
        return 1

    async def sync_archive(self, user_id: int, session, time_service, frames):
        archived_tasks = await get_archived_tasks(session, user_id)
        reminders = await get_active_reminders(session, user_id)
        blocks = await get_active_problem_blocks(session, user_id, time_service.now())
        n = 0
        for i, t in enumerate(archived_tasks[:20]):
            c = f"[АРХИВ]\nТип: task\nНазвание: {t.title}\nПричина: {t.cleanup_reason or '-'}\nДата: {t.archived_at or '-'}"
            await self.create_or_update_item(t.miro_item_id, c, frames["archive"]["x"] + 250, frames["archive"]["y"] + 380 + i * 220, "gray")
            n += 1
        # summary for non-active reminders/problem blocks (MVP summary to avoid clutter)
        c2 = f"[АРХИВ]\nСводка:\nактивные напоминания: {len(reminders)}\nактивные блоки: {len(blocks)}"
        await self.create_or_update_item("", c2, frames["archive"]["x"] + 250, frames["archive"]["y"] + 380 + n * 220, "light_gray")
        return n

    async def create_or_update_item(self, item_id: str, content: str, x: int, y: int, color: str) -> str | None:
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
