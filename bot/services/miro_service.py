from __future__ import annotations

"""
MiroService — visual HQ panel for 813Assistant.

Concept:
  Telegram bot = Operator  (receives data, makes decisions, persists entities)
  Miro board   = Illustrator (shows state, never invents entities)

Layout (3-column grid):

  Row 0:  [ШТАБ/TODAY]  [СЛЕДУЮЩИЙ ШАГ]  [РИСКИ]
  Row 1:  [ЗАДАЧИ]      [НАПОМИНАНИЯ]    [ПРОБЛЕМЫ]
  Row 2:  [РАСПИСАНИЕ]  [CHECK-INS]      [АРХИВ]
  Row 3:  [ДЕНЬГИ] [ЗАКАЗЫ] [УЧЁБА] [ТЕЛО] [ПРОТОКОЛЫ]

Each section has:
  - A shape-based header bar (wide, colored, bold label + "updated HH:MM")
  - Sticky note cards for content items
  - Empty-state sticky if no data

Duplicates prevented by stable entity_type + entity_id stored in miro_mappings.
"""

import logging
from datetime import datetime
from typing import Literal

import httpx

from bot.database.queries import (
    get_active_problem_blocks,
    get_active_reminders,
    get_active_tasks,
    get_archived_tasks,
    get_done_reminders_count,
    get_archived_problem_blocks,
    get_or_create_miro_mapping,
    get_or_create_runtime_state,
    get_upcoming_overrides,
)

logger = logging.getLogger(__name__)

# ── Grid constants ─────────────────────────────────────────────────────────────
COL_STRIDE = 2100       # column pitch  (section_w=1800 + gap=300)
ROW_STRIDE = 1700       # row pitch     (section_h=1400 + gap=300)

SHAPE_W = 1800          # section header shape width
SHAPE_H = 100           # section header shape height

CARD_START_DY = 160     # first card Y offset from section origin
CARD_STRIDE_DY = 380    # vertical pitch between consecutive cards

MAX_TASKS = 7
MAX_REMINDERS = 5
MAX_PROBLEMS = 5
MAX_SCHEDULE = 5

# Board-header shape: above row-0, spanning 3 columns
BOARD_HEADER_DY = -500  # relative to base_y
BOARD_HEADER_W = COL_STRIDE * 3 - 300   # 6000

# ── Section definitions ────────────────────────────────────────────────────────
#   key -> (col, row, label, hex_fill, hex_text)
SECTIONS: dict[str, tuple[int, int, str, str, str]] = {
    "today":     (0, 0, "ШТАБ / TODAY",        "#7b44d8", "#ffffff"),
    "next":      (1, 0, "СЛЕДУЮЩИЙ ШАГ",        "#2d9bf0", "#ffffff"),
    "risks":     (2, 0, "РИСКИ",               "#d32f2f", "#ffffff"),
    "tasks":     (0, 1, "ЗАДАЧИ",              "#f6c000", "#1a1a1a"),
    "reminders": (1, 1, "НАПОМИНАНИЯ",          "#64b5f6", "#1a1a1a"),
    "problems":  (2, 1, "ПРОБЛЕМНЫЕ БЛОКИ",     "#f57c00", "#ffffff"),
    "schedule":  (0, 2, "РАСПИСАНИЕ",           "#66bb6a", "#1a1a1a"),
    "checkins":  (1, 2, "CHECK-INS",            "#66bb6a", "#1a1a1a"),
    "archive":   (2, 2, "АРХИВ",               "#90a4ae", "#ffffff"),
    "money":     (0, 3, "ДЕНЬГИ",              "#b0bec5", "#1a1a1a"),
    "orders":    (1, 3, "ЗАКАЗЫ",              "#b0bec5", "#1a1a1a"),
    "study":     (2, 3, "УЧЁБА",              "#b0bec5", "#1a1a1a"),
    "body":      (3, 3, "ТЕЛО",               "#b0bec5", "#1a1a1a"),
    "protocols": (4, 3, "ПРОТОКОЛЫ",           "#b0bec5", "#1a1a1a"),
}

# ── Sticky note color whitelist ────────────────────────────────────────────────
# Only these values accepted by Miro v2 sticky_notes API
_STICKY_COLORS = {
    "red", "light_pink", "dark_red",
    "yellow", "light_yellow",
    "light_green", "green",
    "light_blue", "blue",
    "violet",
    "light_gray",
    "white",
    "orange",
}

def _safe_sticky_color(color: str) -> str:
    return color if color in _STICKY_COLORS else "light_gray"

def _safe_content(text: str | None, max_len: int = 1000) -> str:
    if not text or not isinstance(text, str):
        return "—"
    text = text.strip()
    if not text:
        return "—"
    return text[:max_len - 3] + "..." if len(text) > max_len else text

# ── Stable entity IDs for sections (never change → no duplicates on re-sync) ──
# Range 5000-5999 reserved for Miro structural elements.
_SECTION_ENTITY_IDS: dict[str, int] = {
    "board_header":   5000,
    "today_header":   5001,  "today_card":     5002,
    "next_header":    5003,  "next_card":      5004,
    "risks_header":   5005,  "risks_card":     5006,
    "tasks_header":   5007,
    "reminders_header": 5008,
    "problems_header":  5009,
    "schedule_header":  5010,
    "checkins_header":  5011,  "checkins_card":  5012,
    "archive_header":   5013,  "archive_card":   5014,
    "money_header":     5015,  "money_card":     5016,
    "orders_header":    5017,  "orders_card":    5018,
    "study_header":     5019,  "study_card":     5020,
    "body_header":      5021,  "body_card":      5022,
    "protocols_header": 5023,  "protocols_card": 5024,
}


class MiroService:

    def __init__(self, token: str, board_id: str, start_x: int, start_y: int):
        self.token = token
        self.board_id = board_id
        self.start_x = start_x
        self.start_y = start_y

    def is_configured(self) -> bool:
        return bool(self.token and self.board_id)

    # ── Position helpers ──────────────────────────────────────────────────────

    def _section_xy(self, key: str) -> tuple[int, int]:
        col, row, *_ = SECTIONS[key]
        x = self.start_x + col * COL_STRIDE
        y = self.start_y + row * ROW_STRIDE
        return x, y

    def _card_xy(self, section_key: str, card_index: int) -> tuple[int, int]:
        sx, sy = self._section_xy(section_key)
        return sx, sy + CARD_START_DY + card_index * CARD_STRIDE_DY

    def _board_header_xy(self) -> tuple[int, int]:
        return self.start_x, self.start_y + BOARD_HEADER_DY

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _safe_request(
        self,
        method: Literal["post", "patch", "delete"],
        url: str,
        payload: dict | None,
        entity_type: str,
        entity_id: int,
        stats: dict,
    ) -> tuple[str | None, str]:
        """
        Execute a Miro API request safely.
        Returns (item_id, op) where op = "created" | "updated" | "deleted" | "error".
        Never raises. Logs errors without exposing the token.
        """
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                fn = getattr(client, method)
                r = await fn(url, headers=self._headers(), json=payload)
        except httpx.RequestError as exc:
            logger.error("Miro network error [%s id=%s %s]: %s", entity_type, entity_id, url, exc)
            stats["errors"] += 1
            return None, "error"

        # Success paths
        if method == "post" and r.status_code == 201:
            item_id = r.json().get("id", "")
            logger.debug("Miro POST 201 [%s id=%s] → %s", entity_type, entity_id, item_id)
            return item_id, "created"

        if method == "patch" and r.status_code < 400:
            return None, "updated"  # item_id unchanged

        if method == "delete" and r.status_code in (200, 204):
            return None, "deleted"

        # Error — log full detail (no token)
        content_hint = ""
        if payload:
            data = payload.get("data", {})
            content_hint = str(data.get("content", ""))[:80].replace("\n", " ")
        logger.error(
            "Miro %s FAIL [status=%d entity=%s id=%s] url=%s content=%r body=%s",
            method.upper(), r.status_code, entity_type, entity_id,
            url, content_hint, r.text[:400],
        )
        stats["errors"] += 1
        return None, "error"

    # ── Shape (section header) ────────────────────────────────────────────────

    async def _create_or_update_shape(
        self,
        item_id: str,
        content: str,
        x: int, y: int,
        width: int, height: int,
        fill_hex: str,
        text_hex: str,
        entity_type: str,
        entity_id: int,
        stats: dict,
    ) -> tuple[str | None, str]:
        payload = {
            "data": {"content": _safe_content(content), "format": "plain"},
            "style": {
                "fillColor": fill_hex,
                "fillOpacity": "1.0",
                "fontFamily": "roboto",
                "fontSize": "18",
                "textAlign": "left",
                "textAlignVertical": "middle",
                "color": text_hex,
                "borderStyle": "normal",
                "borderOpacity": "0.0",
                "borderColor": fill_hex,
                "borderWidth": "1",
            },
            "geometry": {"width": width, "height": height, "rotation": 0.0},
            "position": {"x": x, "y": y, "origin": "start"},
        }
        base = f"https://api.miro.com/v2/boards/{self.board_id}/shapes"

        # Try PATCH first (update existing)
        if item_id:
            _, op = await self._safe_request("patch", f"{base}/{item_id}", payload, entity_type, entity_id, stats)
            if op == "updated":
                stats["updated"] += 1
                return item_id, "updated"
            # Fall through to POST (item may have been deleted from board)

        # POST (create)
        new_id, op = await self._safe_request("post", base, payload, entity_type, entity_id, stats)
        if op == "created":
            stats["created"] += 1
        return new_id, op

    # ── Sticky note (content card) ────────────────────────────────────────────

    async def _create_or_update_sticky(
        self,
        item_id: str,
        content: str,
        x: int, y: int,
        color: str,
        entity_type: str,
        entity_id: int,
        stats: dict,
    ) -> tuple[str | None, str]:
        payload = {
            "data": {"content": _safe_content(content)},
            "position": {"x": x, "y": y},
            "style": {"fillColor": _safe_sticky_color(color)},
        }
        base = f"https://api.miro.com/v2/boards/{self.board_id}/sticky_notes"

        if item_id:
            _, op = await self._safe_request("patch", f"{base}/{item_id}", payload, entity_type, entity_id, stats)
            if op == "updated":
                stats["updated"] += 1
                return item_id, "updated"

        new_id, op = await self._safe_request("post", base, payload, entity_type, entity_id, stats)
        if op == "created":
            stats["created"] += 1
        return new_id, op

    # ── Mapping helpers ───────────────────────────────────────────────────────

    async def _get_mapping(self, session, user_id: int, entity_type: str, entity_id: int):
        return await get_or_create_miro_mapping(session, user_id, entity_type, entity_id, self.board_id)

    async def _save_mapping(self, mp, item_id: str, x: int, y: int):
        if item_id:
            mp.item_id = item_id
        mp.x = x
        mp.y = y

    # ── Section header render ─────────────────────────────────────────────────

    async def _render_section_header(
        self,
        session,
        user_id: int,
        section_key: str,
        updated_at: str,
        stats: dict,
    ) -> None:
        col, row, label, fill_hex, text_hex = SECTIONS[section_key]
        x, y = self._section_xy(section_key)
        entity_type = "miro_section_header"
        entity_id = _SECTION_ENTITY_IDS[f"{section_key}_header"]
        mp = await self._get_mapping(session, user_id, entity_type, entity_id)
        content = f"  {label}    обновлено: {updated_at}"
        new_id, op = await self._create_or_update_shape(
            mp.item_id, content, x, y, SHAPE_W, SHAPE_H, fill_hex, text_hex,
            entity_type, entity_id, stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        stats["sections"] += 1

    # ── Board header ──────────────────────────────────────────────────────────

    async def _render_board_header(self, session, user_id: int, now: datetime, stats: dict) -> None:
        x, y = self._board_header_xy()
        entity_type = "miro_board_header"
        entity_id = _SECTION_ENTITY_IDS["board_header"]
        mp = await self._get_mapping(session, user_id, entity_type, entity_id)
        content = f"  813ASSISTANT / AI-ШТАБ    {now.strftime('%Y-%m-%d %H:%M')}"
        new_id, op = await self._create_or_update_shape(
            mp.item_id, content, x, y, BOARD_HEADER_W, 130,
            "#1a1a1a", "#ffffff", entity_type, entity_id, stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)

    # ── Section: TODAY ────────────────────────────────────────────────────────

    async def _render_today_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "today", t_str, stats)

        tasks = await get_active_tasks(session, user_id)
        reminders = await get_active_reminders(session, user_id)
        blocks = await get_active_problem_blocks(session, user_id, now)
        overrides = await get_upcoming_overrides(session, user_id, now.date())
        state = await get_or_create_runtime_state(session, user_id)

        mode = "обычный"
        if any(str(o.date) == str(now.date()) and o.mode == "rest_day" for o in overrides):
            mode = "отдых"
        if state.quiet_until and state.quiet_until > now:
            mode = "тихий"

        risks = []
        if any(t.deadline and t.deadline < now for t in tasks):
            risks.append("просрочка")
        if mode == "тихий":
            risks.append("тихий режим")

        focus = tasks[0].title if tasks else "не задан"
        content = (
            f"ШТАБ\n{now.strftime('%Y-%m-%d')}\n\n"
            f"режим: {mode}\nфокус: {focus}\n\n"
            f"задачи: {len(tasks)}\n"
            f"напоминания: {len(reminders)}\n"
            f"блоки: {len(blocks)}\n"
            f"риски: {', '.join(risks) if risks else 'нет'}"
        )
        x, y = self._card_xy("today", 0)
        mp = await self._get_mapping(session, user_id, "miro_today_card", _SECTION_ENTITY_IDS["today_card"])
        new_id, _ = await self._create_or_update_sticky(
            mp.item_id, content, x, y, "violet", "miro_today_card",
            _SECTION_ENTITY_IDS["today_card"], stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        stats["cards"] += 1

    # ── Section: NEXT STEP ────────────────────────────────────────────────────

    async def _render_next_section(self, session, user_id: int, time_service, next_step_service, stats: dict) -> None:
        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "next", t_str, stats)

        payload = await next_step_service.build_next_step(user_id, session, now)
        actions = payload.get("actions", [])[:3]
        mode = payload.get("mode", "normal")

        if not actions:
            body = "нет активных действий"
        else:
            body = "\n".join(f"{i+1}. {a}" for i, a in enumerate(actions))

        content = f"СЛЕДУЮЩИЙ ШАГ\n\nрежим: {mode}\n\n{body}"
        x, y = self._card_xy("next", 0)
        mp = await self._get_mapping(session, user_id, "miro_next_card", _SECTION_ENTITY_IDS["next_card"])
        new_id, _ = await self._create_or_update_sticky(
            mp.item_id, content, x, y, "light_blue", "miro_next_card",
            _SECTION_ENTITY_IDS["next_card"], stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        stats["cards"] += 1

    # ── Section: RISKS ────────────────────────────────────────────────────────

    async def _render_risks_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "risks", t_str, stats)

        tasks = await get_active_tasks(session, user_id)
        state = await get_or_create_runtime_state(session, user_id)
        overrides = await get_upcoming_overrides(session, user_id, now.date())

        risks = []
        overdue = [t for t in tasks if t.deadline and t.deadline < now]
        if overdue:
            risks.append(f"просрочка: {len(overdue)} задач")
            for t in overdue[:3]:
                risks.append(f"  — {t.title[:50]}")
        if state.quiet_until and state.quiet_until > now:
            risks.append(f"тихий режим до {state.quiet_until.strftime('%H:%M')}")
        if not state.checkin_enabled:
            risks.append("check-ins отключены")
        if any(str(o.date) == str(now.date()) and o.mode == "rest_day" for o in overrides):
            risks.append("день отдыха: не перегружать")

        content = "РИСКИ\n\n" + ("\n".join(risks) if risks else "рисков нет")
        x, y = self._card_xy("risks", 0)
        mp = await self._get_mapping(session, user_id, "miro_risks_card", _SECTION_ENTITY_IDS["risks_card"])
        color = "red" if risks else "light_green"
        new_id, _ = await self._create_or_update_sticky(
            mp.item_id, content, x, y, color, "miro_risks_card",
            _SECTION_ENTITY_IDS["risks_card"], stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        stats["cards"] += 1

    # ── Section: TASKS ────────────────────────────────────────────────────────

    async def _render_tasks_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "tasks", now.strftime("%H:%M"), stats)

        tasks = sorted(
            await get_active_tasks(session, user_id),
            key=lambda t: (0 if t.priority in {"urgent", "high"} else 1 if t.priority == "medium" else 2)
        )

        if not tasks:
            x, y = self._card_xy("tasks", 0)
            mp = await self._get_mapping(session, user_id, "miro_tasks_empty", _SECTION_ENTITY_IDS["tasks_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "нет активных задач", x, y, "light_gray",
                "miro_tasks_empty", _SECTION_ENTITY_IDS["tasks_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        display = tasks[:MAX_TASKS]
        for i, task in enumerate(display):
            color = "red" if task.priority in {"urgent", "high"} else ("yellow" if task.priority == "medium" else "light_gray")
            dl = task.deadline.strftime("%Y-%m-%d") if task.deadline else "без дедлайна"
            content = f"ЗАДАЧА\n{task.title}\n\npriority: {task.priority}\ndeadline: {dl}"
            if task.deadline and task.deadline < now:
                content += "\n⚠ ПРОСРОЧЕНО"
            x, y = self._card_xy("tasks", i)
            mp = await self._get_mapping(session, user_id, "task", task.id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, color, "task", task.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

        extra = len(tasks) - MAX_TASKS
        if extra > 0:
            x, y = self._card_xy("tasks", MAX_TASKS)
            mp = await self._get_mapping(session, user_id, "miro_tasks_overflow", _SECTION_ENTITY_IDS["tasks_header"] + 99)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, f"+ ещё {extra} задач в базе", x, y, "light_gray",
                "miro_tasks_overflow", _SECTION_ENTITY_IDS["tasks_header"] + 99, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: REMINDERS ────────────────────────────────────────────────────

    async def _render_reminders_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "reminders", now.strftime("%H:%M"), stats)

        reminders = sorted(
            await get_active_reminders(session, user_id),
            key=lambda r: (0 if r.remind_at <= now else 1, r.remind_at)
        )

        if not reminders:
            x, y = self._card_xy("reminders", 0)
            mp = await self._get_mapping(session, user_id, "miro_rem_empty", _SECTION_ENTITY_IDS["reminders_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "нет активных напоминаний", x, y, "light_gray",
                "miro_rem_empty", _SECTION_ENTITY_IDS["reminders_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        for i, r in enumerate(reminders[:MAX_REMINDERS]):
            overdue = r.remind_at <= now
            color = "red" if overdue else "light_blue"
            when = r.remind_at.strftime("%Y-%m-%d %H:%M")
            content = f"НАПОМИНАНИЕ\n{r.text}\n\nкогда: {when}"
            if overdue:
                content += "\n⚠ просрочено"
            x, y = self._card_xy("reminders", i)
            mp = await self._get_mapping(session, user_id, "reminder", r.id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, color, "reminder", r.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: PROBLEMS ─────────────────────────────────────────────────────

    async def _render_problems_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "problems", now.strftime("%H:%M"), stats)

        blocks = await get_active_problem_blocks(session, user_id, now)

        if not blocks:
            x, y = self._card_xy("problems", 0)
            mp = await self._get_mapping(session, user_id, "miro_prob_empty", _SECTION_ENTITY_IDS["problems_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "нет открытых блоков", x, y, "light_green",
                "miro_prob_empty", _SECTION_ENTITY_IDS["problems_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        for i, b in enumerate(blocks[:MAX_PROBLEMS]):
            color = "red" if (b.priority in {"urgent", "high"} or b.pressure_level == "hard") else "orange"
            next_act = (b.next_action[:60] + "...") if len(b.next_action or "") > 60 else (b.next_action or "—")
            content = f"БЛОК\n{b.title}\n\nкатегория: {b.category}\nследующий шаг:\n{next_act}"
            x, y = self._card_xy("problems", i)
            mp = await self._get_mapping(session, user_id, "problem_block", b.id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, color, "problem_block", b.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: SCHEDULE ─────────────────────────────────────────────────────

    async def _render_schedule_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "schedule", now.strftime("%H:%M"), stats)

        overrides = await get_upcoming_overrides(session, user_id, now.date())

        if not overrides:
            x, y = self._card_xy("schedule", 0)
            mp = await self._get_mapping(session, user_id, "miro_sched_empty", _SECTION_ENTITY_IDS["schedule_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "плановых исключений нет", x, y, "light_gray",
                "miro_sched_empty", _SECTION_ENTITY_IDS["schedule_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        for i, o in enumerate(overrides[:MAX_SCHEDULE]):
            color = "light_green" if o.mode == "rest_day" else "light_blue"
            content = f"РАСПИСАНИЕ\n{o.date}\n\nрежим: {o.mode}\n{o.description or ''}"
            x, y = self._card_xy("schedule", i)
            mp = await self._get_mapping(session, user_id, "schedule_override", o.id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, color, "schedule_override", o.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: CHECK-INS ────────────────────────────────────────────────────

    async def _render_checkins_section(self, session, user_id: int, time_service, cfg, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "checkins", now.strftime("%H:%M"), stats)

        state = await get_or_create_runtime_state(session, user_id)
        quiet = state.quiet_until.strftime("%H:%M") if state.quiet_until and state.quiet_until > now else "нет"
        last = state.last_checkin_at.strftime("%Y-%m-%d %H:%M") if state.last_checkin_at else "—"

        status = "включены" if state.checkin_enabled else "выключены"
        color = "light_green" if state.checkin_enabled else "light_gray"

        content = (
            f"CHECK-INS\n\nстатус: {status}\n"
            f"утро: {cfg.checkin_morning_time}\n"
            f"день: {cfg.checkin_day_time}\n"
            f"вечер: {cfg.checkin_evening_time}\n\n"
            f"тихий режим: {quiet}\n"
            f"последний: {last}"
        )
        x, y = self._card_xy("checkins", 0)
        mp = await self._get_mapping(session, user_id, "miro_checkins_card", _SECTION_ENTITY_IDS["checkins_card"])
        new_id, _ = await self._create_or_update_sticky(
            mp.item_id, content, x, y, color, "miro_checkins_card",
            _SECTION_ENTITY_IDS["checkins_card"], stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        stats["cards"] += 1

    # ── Section: ARCHIVE ──────────────────────────────────────────────────────

    async def _render_archive_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "archive", now.strftime("%H:%M"), stats)

        archived_tasks = await get_archived_tasks(session, user_id)
        archived_blocks = await get_archived_problem_blocks(session, user_id)
        done_rem = await get_done_reminders_count(session, user_id)

        # Summary card
        content = (
            f"АРХИВ\n\n"
            f"задачи: {len(archived_tasks)}\n"
            f"напоминания (завершено): {done_rem}\n"
            f"блоки: {len(archived_blocks)}"
        )
        x, y = self._card_xy("archive", 0)
        mp = await self._get_mapping(session, user_id, "miro_archive_card", _SECTION_ENTITY_IDS["archive_card"])
        new_id, _ = await self._create_or_update_sticky(
            mp.item_id, content, x, y, "light_gray", "miro_archive_card",
            _SECTION_ENTITY_IDS["archive_card"], stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        stats["cards"] += 1

        # Recent archived tasks (up to 5)
        for i, t in enumerate(archived_tasks[:5]):
            x, y = self._card_xy("archive", i + 1)
            mp = await self._get_mapping(session, user_id, "archived_task", t.id)
            content = f"ЗАКРЫТО\n{t.title}\n\npричина: {t.cleanup_reason or '—'}"
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, "light_gray", "archived_task", t.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: FUTURE (placeholders) ───────────────────────────────────────

    async def _render_future_sections(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        t_str = now.strftime("%H:%M")

        tasks = await get_active_tasks(session, user_id)
        blocks = await get_active_problem_blocks(session, user_id, now)

        money_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["оплата", "деньги", "платёж"]))
        orders_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["заказ", "клиент", "доставка"]))
        study_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["учёба", "экзамен", "егэ", "ошибка"]))
        body_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["сон", "тело", "боль", "перегруз"]))

        future_map = {
            "money":     (money_tasks,  "ДЕНЬГИ",     "платежи и расчёты"),
            "orders":    (orders_tasks, "ЗАКАЗЫ",     "клиенты и доставки"),
            "study":     (study_tasks,  "УЧЁБА",      "подготовка и ошибки"),
            "body":      (body_tasks,   "ТЕЛО",       "сон и восстановление"),
            "protocols": (0,            "ПРОТОКОЛЫ",  "сценарии поведения"),
        }

        for key, (n_tasks, label, focus) in future_map.items():
            await self._render_section_header(session, user_id, key, t_str, stats)
            x, y = self._card_xy(key, 0)
            mp = await self._get_mapping(
                session, user_id,
                f"miro_{key}_card",
                _SECTION_ENTITY_IDS[f"{key}_card"],
            )
            content = f"{label}\n\nфокус: {focus}\nактивных задач: {n_tasks}\nактивных блоков: {len(blocks)}"
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, "light_gray",
                f"miro_{key}_card", _SECTION_ENTITY_IDS[f"{key}_card"], stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Public: sync_all ──────────────────────────────────────────────────────

    async def sync_all(self, user_id: int, session, time_service, cfg, next_step_service=None) -> dict:
        stats = {"sections": 0, "cards": 0, "created": 0, "updated": 0, "errors": 0}
        now = time_service.now()

        await self._render_board_header(session, user_id, now, stats)
        await self._render_today_section(session, user_id, time_service, stats)
        await self._render_next_section(session, user_id, time_service, next_step_service, stats)
        await self._render_risks_section(session, user_id, time_service, stats)
        await self._render_tasks_section(session, user_id, time_service, stats)
        await self._render_reminders_section(session, user_id, time_service, stats)
        await self._render_problems_section(session, user_id, time_service, stats)
        await self._render_schedule_section(session, user_id, time_service, stats)
        await self._render_checkins_section(session, user_id, time_service, cfg, stats)
        await self._render_archive_section(session, user_id, time_service, stats)
        await self._render_future_sections(session, user_id, time_service, stats)

        return stats

    # ── Debug helpers ─────────────────────────────────────────────────────────

    async def debug_get_items(self) -> dict:
        """GET /v2/boards/{id}/items?limit=10 for connectivity check (Miro min page size = 10)."""
        url = f"https://api.miro.com/v2/boards/{self.board_id}/items?limit=10"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=self._headers())
            return {"status_code": r.status_code, "error": r.text[:200] if r.status_code >= 400 else None}
        except Exception as exc:
            return {"status_code": None, "error": str(exc)[:200]}

    async def debug_create_test_note(self) -> dict:
        """Create one test sticky note (only invoked by /miro_debug). Never invoked by sync_all."""
        x, y = self.start_x, self.start_y - 800  # above board header, out of the way
        payload = {
            "data": {"content": "813Assistant debug\n/miro_debug"},
            "position": {"x": x, "y": y},
            "style": {"fillColor": "light_green"},
        }
        url = f"https://api.miro.com/v2/boards/{self.board_id}/sticky_notes"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(url, headers=self._headers(), json=payload)
            return {
                "status_code": r.status_code,
                "item_id": r.json().get("id") if r.status_code == 201 else None,
                "error": r.text[:300] if r.status_code >= 400 else None,
            }
        except Exception as exc:
            return {"status_code": None, "item_id": None, "error": str(exc)[:200]}
