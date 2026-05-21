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

# ── Miro API v2 fillColor whitelist ───────────────────────────────────────────
# https://developers.miro.com/reference/create-sticky-note
# Only these exact string values are accepted.
VALID_FILL_COLORS = {
    "red", "light_pink", "dark_red",
    "yellow", "light_yellow",
    "light_green", "green",
    "light_blue", "blue",
    "violet",
    "light_gray", "gray", "black",
    "white",
    "orange",
}

# Aliases for colors that are NOT in the whitelist → safe fallback
COLOR_ALIAS: dict[str, str] = {
    "light_yellow": "light_yellow",  # valid
    "gray": "light_gray",            # "gray" is NOT valid in v2 — map to light_gray
    "red": "red",
    "yellow": "yellow",
    "light_blue": "light_blue",
    "light_green": "light_green",
    "light_gray": "light_gray",
    "white": "white",
    "orange": "orange",
    "violet": "violet",
}

MAX_CONTENT_LEN = 1500  # Miro sticky note content limit (safe margin)


def _safe_color(color: str) -> str:
    """Map potentially invalid color name to a whitelisted Miro v2 value."""
    mapped = COLOR_ALIAS.get(color, "light_gray")
    if mapped not in VALID_FILL_COLORS:
        return "light_gray"
    return mapped


def _safe_content(content: str | None) -> str:
    """Ensure content is a non-empty string within safe length."""
    if not content or not isinstance(content, str):
        return "Без названия"
    content = content.strip()
    if not content:
        return "Без названия"
    if len(content) > MAX_CONTENT_LEN:
        content = content[:MAX_CONTENT_LEN - 3] + "..."
    return content


def _safe_position(x, y, start_x: int) -> tuple[int, int]:
    """Ensure x/y are valid integers >= start_x."""
    try:
        x = int(x)
    except (TypeError, ValueError):
        x = start_x
    try:
        y = int(y)
    except (TypeError, ValueError):
        y = 0
    if x < start_x:
        x = start_x
    return x, y


class MiroService:
    FRAME_ORDER = [
        ("today_summary", "ШТАБ / TODAY", 0),
        ("tasks", "ЗАДАЧИ / TASKS", 3000),
        ("reminders", "НАПОМИНАНИЯ / REMINDERS", 6000),
        ("problem_blocks", "ПРОБЛЕМЫ / PROBLEM BLOCKS", 9000),
        ("schedule", "РАСПИСАНИЕ / SCHEDULE", 12000),
        ("checkin_summary", "CHECK-INS", 15000),
        ("archive", "АРХИВ / ARCHIVE", 18000),
    ]

    def __init__(self, token: str, board_id: str, start_x: int, start_y: int):
        self.token = token
        self.board_id = board_id
        self.start_x = start_x
        self.start_y = start_y

    def is_configured(self) -> bool:
        return bool(self.token and self.board_id)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    # ── Public: sync_all ──────────────────────────────────────────────────────

    async def sync_all(self, user_id: int, session, time_service, cfg) -> dict:
        stats = {
            "tasks": 0, "reminders": 0, "problems": 0, "archive": 0,
            "created": 0, "updated": 0, "errors": 0,
        }
        frames = await self.ensure_frames(user_id, session, stats)
        await self.sync_today_frame(user_id, session, time_service, frames, stats)
        stats["tasks"] = await self.sync_tasks(user_id, session, time_service, frames, stats)
        stats["reminders"] = await self.sync_reminders(user_id, session, time_service, frames, stats)
        stats["problems"] = await self.sync_problem_blocks(user_id, session, time_service, frames, stats)
        await self.sync_schedule(user_id, session, time_service, frames, stats)
        await self.sync_checkins(user_id, session, time_service, cfg, frames, stats)
        stats["archive"] = await self.sync_archive(user_id, session, time_service, frames, stats)
        return stats

    # ── Frames ────────────────────────────────────────────────────────────────

    async def ensure_frames(self, user_id: int, session, stats: dict) -> dict:
        out = {}
        for key, title, offset in self.FRAME_ORDER:
            x = self.start_x + offset
            y = self.start_y
            mp = await get_or_create_miro_mapping(session, user_id, "frame_header", offset, self.board_id)
            item_id, op = await self.create_or_update_item(
                mp.item_id, f"[{title}]", x, y, "light_gray",
                entity_type="frame_header", entity_id=offset,
            )
            if item_id:
                if op == "created":
                    stats["created"] += 1
                    mp.item_id = item_id
                elif op == "updated":
                    stats["updated"] += 1
                mp.x = x
                mp.y = y
            else:
                stats["errors"] += 1
            out[key] = {"x": x, "y": y, "id": item_id}
        return out

    # ── Today summary ─────────────────────────────────────────────────────────

    async def sync_today_frame(self, user_id, session, time_service, frames, stats):
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
            risks.append("check-ins off")
        text = (
            f"[ШТАБ]\nДата: {now.date()}\nРежим: {mode}\n"
            f"Фокус: {tasks[0].title if tasks else 'нет'}\n"
            f"Задачи: {len(tasks)}\nНапоминания: {len(reminders)}\n"
            f"Блоки: {len(blocks)}\nРиски: {', '.join(risks) if risks else 'нет'}"
        )
        mp = await get_or_create_miro_mapping(session, user_id, "today_summary", 0, self.board_id)
        item_id, op = await self.create_or_update_item(
            mp.item_id, text,
            frames["today_summary"]["x"] + 200, frames["today_summary"]["y"] + 450,
            "light_yellow", entity_type="today_summary", entity_id=0,
        )
        if item_id:
            if op == "created":
                stats["created"] += 1
                mp.item_id = item_id
            elif op == "updated":
                stats["updated"] += 1
        else:
            stats["errors"] += 1

    # ── Tasks ─────────────────────────────────────────────────────────────────

    async def sync_tasks(self, user_id, session, time_service, frames, stats) -> int:
        tasks = await get_active_tasks(session, user_id)
        total = len(tasks)
        for i, task in enumerate(tasks[:15]):
            color = "red" if task.priority in {"urgent", "high"} else ("yellow" if task.priority == "medium" else "light_gray")
            dl = task.deadline.isoformat() if task.deadline else "-"
            content = f"[ЗАДАЧА]\n{task.title}\nПриоритет: {task.priority}\nДедлайн: {dl}"
            item_id, op = await self.create_or_update_item(
                task.miro_item_id, content,
                frames["tasks"]["x"] + 250, frames["tasks"]["y"] + 380 + i * 240,
                color, entity_type="task", entity_id=task.id,
            )
            if item_id:
                if op == "created":
                    stats["created"] += 1
                    task.miro_item_id = item_id
                elif op == "updated":
                    stats["updated"] += 1
            else:
                stats["errors"] += 1
        if total > 15:
            await self.create_or_update_item(
                "", f"+ ещё {total - 15} задач в базе",
                frames["tasks"]["x"] + 250, frames["tasks"]["y"] + 380 + 15 * 240,
                "light_gray", entity_type="task_overflow", entity_id=0,
            )
        return min(total, 15)

    # ── Reminders ─────────────────────────────────────────────────────────────

    async def sync_reminders(self, user_id, session, time_service, frames, stats) -> int:
        reminders = sorted(await get_active_reminders(session, user_id), key=lambda r: r.remind_at)[:10]
        now = time_service.now()
        for i, r in enumerate(reminders):
            overdue = r.remind_at <= now
            color = "red" if overdue else "light_blue"
            content = f"[НАПОМИНАНИЕ]\n{r.text}\nКогда: {r.remind_at.strftime('%Y-%m-%d %H:%M')}" + ("\nПросрочено" if overdue else "")
            item_id, op = await self.create_or_update_item(
                r.miro_item_id, content,
                frames["reminders"]["x"] + 250, frames["reminders"]["y"] + 380 + i * 240,
                color, entity_type="reminder", entity_id=r.id,
            )
            if item_id:
                if op == "created":
                    stats["created"] += 1
                    r.miro_item_id = item_id
                elif op == "updated":
                    stats["updated"] += 1
            else:
                stats["errors"] += 1
        return len(reminders)

    # ── Problem blocks ────────────────────────────────────────────────────────

    async def sync_problem_blocks(self, user_id, session, time_service, frames, stats) -> int:
        blocks = await get_active_problem_blocks(session, user_id, time_service.now())
        for i, b in enumerate(blocks[:10]):
            color = "red" if (b.priority in {"urgent", "high"} or b.pressure_level == "hard") else ("yellow" if b.priority == "medium" else "light_blue")
            content = f"[БЛОК]\n{b.title}\nКатегория: {b.category}\nСледующий шаг: {b.next_action}\nДедлайн: {b.deadline or '-'}"
            mp = await get_or_create_miro_mapping(session, user_id, "problem_block", b.id, self.board_id)
            item_id, op = await self.create_or_update_item(
                mp.item_id, content,
                frames["problem_blocks"]["x"] + 250, frames["problem_blocks"]["y"] + 380 + i * 260,
                color, entity_type="problem_block", entity_id=b.id,
            )
            if item_id:
                if op == "created":
                    stats["created"] += 1
                    mp.item_id = item_id
                elif op == "updated":
                    stats["updated"] += 1
            else:
                stats["errors"] += 1
        return min(len(blocks), 10)

    # ── Schedule ──────────────────────────────────────────────────────────────

    async def sync_schedule(self, user_id, session, time_service, frames, stats):
        overrides = await get_upcoming_overrides(session, user_id, time_service.today())
        for i, o in enumerate(overrides[:10]):
            color = "light_green" if o.mode == "rest_day" else "light_blue"
            content = f"[РАСПИСАНИЕ]\nДата: {o.date}\nРежим: {o.mode}\nОписание: {o.description or '-'}"
            mp = await get_or_create_miro_mapping(session, user_id, "schedule_override", o.id, self.board_id)
            item_id, op = await self.create_or_update_item(
                mp.item_id, content,
                frames["schedule"]["x"] + 250, frames["schedule"]["y"] + 380 + i * 240,
                color, entity_type="schedule_override", entity_id=o.id,
            )
            if item_id:
                if op == "created":
                    stats["created"] += 1
                    mp.item_id = item_id
                elif op == "updated":
                    stats["updated"] += 1
            else:
                stats["errors"] += 1

    # ── Check-ins ─────────────────────────────────────────────────────────────

    async def sync_checkins(self, user_id, session, time_service, cfg, frames, stats):
        state = await get_or_create_runtime_state(session, user_id)
        quiet = state.quiet_until.strftime("%Y-%m-%d %H:%M") if state.quiet_until else "нет"
        text = (
            "[CHECK-INS]\n"
            f"Статус: {'enabled' if state.checkin_enabled else 'disabled'}\n"
            f"Тихий режим: {quiet}\n"
            f"Утро: {cfg.checkin_morning_time} / День: {cfg.checkin_day_time} / Вечер: {cfg.checkin_evening_time}"
        )
        color = "light_gray" if not state.checkin_enabled else (
            "light_yellow" if state.quiet_until and state.quiet_until > time_service.now() else "light_green"
        )
        mp = await get_or_create_miro_mapping(session, user_id, "checkin_summary", 0, self.board_id)
        item_id, op = await self.create_or_update_item(
            mp.item_id, text,
            frames["checkin_summary"]["x"] + 250, frames["checkin_summary"]["y"] + 380,
            color, entity_type="checkin_summary", entity_id=0,
        )
        if item_id:
            if op == "created":
                stats["created"] += 1
                mp.item_id = item_id
            elif op == "updated":
                stats["updated"] += 1
        else:
            stats["errors"] += 1

    # ── Archive ───────────────────────────────────────────────────────────────

    async def sync_archive(self, user_id, session, time_service, frames, stats) -> int:
        archived_tasks = await get_archived_tasks(session, user_id)
        n = 0
        for i, t in enumerate(archived_tasks[:20]):
            content = f"[АРХИВ]\nТип: task\n{t.title}\nПричина: {t.cleanup_reason or '-'}"
            item_id, op = await self.create_or_update_item(
                t.miro_item_id, content,
                frames["archive"]["x"] + 250, frames["archive"]["y"] + 380 + i * 220,
                "light_gray", entity_type="archived_task", entity_id=t.id,
            )
            if item_id:
                if op == "created":
                    stats["created"] += 1
                    t.miro_item_id = item_id
                elif op == "updated":
                    stats["updated"] += 1
            else:
                stats["errors"] += 1
            n += 1
        return n

    # ── Core: create_or_update_item ───────────────────────────────────────────

    async def create_or_update_item(
        self,
        item_id: str,
        content: str,
        x: int,
        y: int,
        color: str,
        entity_type: str = "unknown",
        entity_id: int = 0,
    ) -> tuple[str | None, str]:
        """
        Create or update a sticky note on the Miro board.

        Returns:
            (item_id, operation) where operation is "created", "updated", or "error".
            Returns (None, "error") on failure — never raises.
        """
        x, y = _safe_position(x, y, self.start_x)
        content = _safe_content(content)
        fill_color = _safe_color(color)

        # Minimal validated payload — no extra style fields
        payload = {
            "data": {"content": content},
            "position": {"x": x, "y": y},
            "style": {"fillColor": fill_color},
        }

        headers = self._headers()
        base_url = f"https://api.miro.com/v2/boards/{self.board_id}/sticky_notes"

        async with httpx.AsyncClient(timeout=20.0) as client:

            # ── Try PATCH (update) first ───────────────────────────────────
            if item_id:
                try:
                    r = await client.patch(f"{base_url}/{item_id}", headers=headers, json=payload)
                    if r.status_code < 400:
                        return item_id, "updated"
                    # PATCH failed (item deleted from board?) — fall through to POST
                    logger.warning(
                        "Miro PATCH %s → %d (entity=%s id=%s). Will retry as POST. body=%s",
                        item_id, r.status_code, entity_type, entity_id,
                        r.text[:200],
                    )
                except httpx.RequestError as exc:
                    logger.warning("Miro PATCH network error (entity=%s id=%s): %s", entity_type, entity_id, exc)

            # ── POST (create) ──────────────────────────────────────────────
            try:
                r = await client.post(base_url, headers=headers, json=payload)
            except httpx.RequestError as exc:
                logger.error("Miro POST network error (entity=%s id=%s): %s", entity_type, entity_id, exc)
                return None, "error"

            if r.status_code == 201:
                new_id = r.json().get("id", "")
                logger.debug("Miro POST 201 (entity=%s id=%s) → item_id=%s", entity_type, entity_id, new_id)
                return new_id, "created"

            # ── Log full error without exposing the token ──────────────────
            content_preview = content[:100].replace("\n", " ")
            logger.error(
                "Miro POST failed: status=%d entity=%s entity_id=%s x=%d y=%d color=%s "
                "content_preview=%r body=%s",
                r.status_code, entity_type, entity_id, x, y, fill_color,
                content_preview, r.text[:500],
            )
            return None, "error"

    # ── Debug: test single sticky note ───────────────────────────────────────

    async def debug_create_test_note(self) -> dict:
        """
        Create one test sticky note and return detailed result.
        Used by /miro_debug command. Never raises.
        """
        x, y = self.start_x, self.start_y
        payload = {
            "data": {"content": "813Assistant debug"},
            "position": {"x": x, "y": y},
            "style": {"fillColor": "light_green"},
        }
        headers = self._headers()
        url = f"https://api.miro.com/v2/boards/{self.board_id}/sticky_notes"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(url, headers=headers, json=payload)
            return {
                "status_code": r.status_code,
                "item_id": r.json().get("id") if r.status_code == 201 else None,
                "error": r.text[:300] if r.status_code >= 400 else None,
            }
        except Exception as exc:
            return {"status_code": None, "item_id": None, "error": str(exc)[:200]}

    async def debug_get_items(self) -> dict:
        """GET /v2/boards/{id}/items?limit=1 for connectivity check."""
        headers = self._headers()
        url = f"https://api.miro.com/v2/boards/{self.board_id}/items?limit=1"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=headers)
            return {"status_code": r.status_code, "error": r.text[:200] if r.status_code >= 400 else None}
        except Exception as exc:
            return {"status_code": None, "error": str(exc)[:200]}
