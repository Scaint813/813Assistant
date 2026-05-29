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
    get_active_exam_dates,
    get_active_problem_blocks,
    get_active_reminders,
    get_active_study_schedule,
    get_active_tasks,
    get_archived_tasks,
    get_done_reminders_count,
    get_archived_problem_blocks,
    get_or_create_miro_mapping,
    get_or_create_runtime_state,
    get_problem_blocks_by_categories,
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

# ── Study dashboard constants ────────────────────────────────────────────────────────────────
STUDY_SHAPE_W = 2000          # wider header for study sections
STUDY_SHAPE_H = 120

# Cluster: 6 sub-cards per block (3 per row, 2 rows)
CLUSTER_COL_W = 900           # sub-card column width in cluster (increased for readability)
CLUSTER_ROW_H = 700           # sub-card row height in cluster (increased for readability)
CLUSTER_GAP_X = 30            # gap between sub-cards horizontally
CLUSTER_GAP_Y = 30            # gap between sub-card rows
CLUSTER_BLOCK_GAP = 160       # vertical gap between separate blocks

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
    "money":         (0, 3, "ДЕНЬГИ",                  "#b0bec5", "#1a1a1a"),
    "orders":        (1, 3, "ЗАКАЗЫ",                  "#b0bec5", "#1a1a1a"),
    "body":          (2, 3, "ТЕЛО",                    "#b0bec5", "#1a1a1a"),
    "protocols":     (3, 3, "ПРОТОКОЛЫ",               "#b0bec5", "#1a1a1a"),
    # Row 4 — учёба (3 dedicated study sections)
    "exams":         (0, 4, "ЭКЗАМЕНЫ / ДЕДЛАЙНЫ",     "#d32f2f", "#ffffff"),
    "study_schedule":(1, 4, "РАСПИСАНИЕ / РЕПЕТИТОРЫ",  "#2d9bf0", "#ffffff"),
    "study_blocks":  (2, 4, "УЧЕБНЫЕ БЛОКИ",            "#f57c00", "#ffffff"),
    # Row 5 — агрегированные секции
    "subject_plan":  (0, 5, "ПЛАН ПО ПРЕДМЕТАМ",        "#7b44d8", "#ffffff"),
    "next_72h":      (1, 5, "СЛЕДУЮЩИЕ 72 ЧАСА",          "#2d9bf0", "#ffffff"),
    # keep study key for legacy mapping header (hidden, offset far right)
    "study":         (5, 4, "УЧЁБА (legacy)",           "#e0e0e0", "#9e9e9e"),
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

def _hex_to_sticky(hex_color: str) -> str:
    """Map a hex color to the nearest Miro sticky_note color name (best-effort)."""
    MAPPING = {
        "#7b44d8": "violet",
        "#2d9bf0": "light_blue",
        "#d32f2f": "red",
        "#f6c000": "yellow",
        "#64b5f6": "light_blue",
        "#f57c00": "orange",
        "#66bb6a": "light_green",
        "#90a4ae": "light_gray",
        "#b0bec5": "light_gray",
        "#1a1a1a": "light_gray",
        "#e0e0e0": "light_gray",
    }
    return MAPPING.get(hex_color.lower(), "light_gray")

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
    # Study sections (row 4)
    "exams_header":          5025,
    "study_schedule_header": 5026,
    "study_blocks_header":   5027,
    # Aggregated study sections (row 5)
    "subject_plan_header":   5032,
    "next_72h_header":       5033,
    "next_72h_card":         5034,
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
            "data": {"content": _safe_content(content)},
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

        # Fallback: shape API failed → create a sticky_note header instead
        if op == "error":
            logger.warning(
                "Miro shape failed, falling back to sticky_note for entity_type=%s id=%s",
                entity_type, entity_id,
            )
            # Undo the error count since we're retrying
            stats["errors"] = max(0, stats["errors"] - 1)
            sticky_payload = {
                "data": {"content": _safe_content(content, 400)},
                "position": {"x": x, "y": y},
                "style": {"fillColor": _safe_sticky_color(_hex_to_sticky(fill_hex))},
            }
            sticky_url = f"https://api.miro.com/v2/boards/{self.board_id}/sticky_notes"
            new_id2, op2 = await self._safe_request("post", sticky_url, sticky_payload, entity_type, entity_id, stats)
            if op2 == "created":
                stats["created"] += 1
                return new_id2, "created"
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

    async def _render_next_section_safe(self, session, user_id: int, time_service, next_step_service, stats: dict) -> None:
        """Same as _render_next_section but catches NextStepService errors and shows fallback card."""
        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "next", t_str, stats)

        try:
            payload = await next_step_service.build_next_step(user_id, session, now)
            actions = payload.get("actions", [])[:3]
            mode = payload.get("mode", "normal")
            body = "\n".join(f"{i+1}. {a}" for i, a in enumerate(actions)) if actions else "нет активных действий"
        except Exception as _exc:
            logger.exception("NextStepService.build_next_step failed: %s", _exc)
            body = "Следующий шаг временно недоступен"
            mode = "error"

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

    # ── Section: ЭКЗАМЕНЫ ────────────────────────────────────────────────────

    async def _render_exams_section(self, session, user_id: int, time_service, stats: dict) -> None:
        """
        Informative exam cards: subject, date, days left, status, subject-specific focus.
        Deduplicates exams by normalized subject key (keeps closest future date).
        """
        from bot.services.study_problem_plan_builder import exam_focus_text, _days_status
        from bot.services.study_problem_templates import _extract_subject_key

        now = time_service.now()
        today = now.date()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "exams", t_str, stats)

        exams = await get_active_exam_dates(session, user_id)

        if not exams:
            x, y = self._card_xy("exams", 0)
            mp = await self._get_mapping(session, user_id, "miro_exams_empty", _SECTION_ENTITY_IDS["exams_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "Экзамены не зафиксированы.\nНапиши: \"ЕГЭ по обществу 10 июня\"",
                x, y, "light_gray", "miro_exams_empty", _SECTION_ENTITY_IDS["exams_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        # Deduplicate by canonical subject key (closest future exam wins)
        seen_subjects: dict[str, object] = {}
        for e in sorted(exams, key=lambda x: x.exam_date):
            sk = _extract_subject_key(e.subject, e.subject, "")
            if sk not in seen_subjects:
                seen_subjects[sk] = e
        deduped_exams = list(seen_subjects.values())

        _STATUS_LABEL = {
            "urgent": "срочно",
            "high":   "высокий приоритет",
            "medium": "контроль базы",
            "normal": "планомерно",
        }

        for i, e in enumerate(deduped_exams):
            days_left = (e.exam_date - today).days
            subject_key = _extract_subject_key(e.subject, e.subject, "")

            if days_left < 0:
                color = "light_gray"
                days_str = "прошло"
                status_str = "закрыт"
                focus = "проанализировать результаты."
            else:
                status_key = _days_status(days_left)
                status_str = _STATUS_LABEL.get(status_key, "планомерно")
                focus = exam_focus_text(subject_key, days_left)
                if days_left <= 7:
                    color = "red"
                    days_str = f"ОСТАЛОСЬ {days_left} ДН.!"
                elif days_left <= 14:
                    color = "red"
                    days_str = f"осталось {days_left} дн."
                elif days_left <= 30:
                    color = "orange"
                    days_str = f"осталось {days_left} дн."
                else:
                    color = "light_blue"
                    days_str = f"осталось {days_left} дн."

            content = (
                f"ЭКЗАМЕН\n{e.subject}\n\n"
                f"Дата: {e.exam_date.strftime('%d.%m')}\n"
                f"Осталось: {days_str}\n"
                f"Статус: {status_str}\n\n"
                f"Фокус:\n{focus}"
            )
            x, y = self._card_xy("exams", i)
            mp = await self._get_mapping(session, user_id, "exam_date", e.id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, color, "exam_date", e.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: РАСПИСАНИЕ / РЕПЕТИТОРЫ ─────────────────────────────────────

    async def _render_study_schedule_section(self, session, user_id: int, time_service, stats: dict) -> None:
        """
        Tutor session cards: subject, day+time, tutor name, subject-specific preparation hints.
        """
        from bot.services.study_problem_plan_builder import tutor_prepare_hints
        from bot.services.study_problem_templates import _extract_subject_key

        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "study_schedule", t_str, stats)

        schedule = await get_active_study_schedule(session, user_id)

        if not schedule:
            x, y = self._card_xy("study_schedule", 0)
            mp = await self._get_mapping(session, user_id, "miro_sched_empty",
                                         _SECTION_ENTITY_IDS["study_schedule_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "Занятия не зафиксированы.\nНапиши: \"репетитор по англ по вторникам 18:00\"",
                x, y, "light_gray", "miro_sched_empty",
                _SECTION_ENTITY_IDS["study_schedule_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        _WDAY = {"mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
                 "fri": "Пт", "sat": "Сб", "sun": "Вс"}

        for i, s in enumerate(schedule):
            wd = _WDAY.get(s.weekday or "", s.weekday or "")
            t = s.time_str or ""
            subject_key = _extract_subject_key(s.subject or "", s.subject or "", "")
            hints = tutor_prepare_hints(subject_key)
            hints_text = "\n".join(f"{j}. {h}" for j, h in enumerate(hints, 1))

            tutor_line = f"\n{s.tutor_name}" if s.tutor_name else ""
            content = (
                f"ЗАНЯТИЕ\n{s.subject}\n\n"
                f"{wd} {t}{tutor_line}\n\n"
                f"Что подготовить:\n{hints_text}"
            )
            x, y = self._card_xy("study_schedule", i)
            mp = await self._get_mapping(session, user_id, "study_schedule_item", s.id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, "light_green", "study_schedule_item", s.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1


    # ── Section: УЧЕБНЫЕ БЛОКИ ────────────────────────────────────────────────

    async def _render_study_blocks_section(self, session, user_id: int, time_service, stats: dict) -> None:
        """
        Deep cluster for each study/exam problem block: 6 cards (3x2 grid).
        Row A: ПРОБЛЕМА | ДИАГНОСТИКА | ТЕМЫ
        Row B: ПРАКТИКА | КОНТРОЛЬ    | СЛЕДУЮЩИЙ ШАГ
        Content built via StudyProblemPlanBuilder — NO raw transcript in Miro.
        Entity type: problem_block_card, entity_id = block.id * 10 + slot_offset (0-5).
        """
        from bot.services.study_problem_plan_builder import build_plan

        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "study_blocks", t_str, stats)

        blocks = await get_problem_blocks_by_categories(session, user_id, ["exam", "study"], now)

        if not blocks:
            x, y = self._card_xy("study_blocks", 0)
            mp = await self._get_mapping(session, user_id, "miro_study_bl_empty",
                                         _SECTION_ENTITY_IDS["study_blocks_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "Учебных блоков нет.\nСоздай блок: \"проблема с заданием 21\"",
                x, y, "light_gray", "miro_study_bl_empty",
                _SECTION_ENTITY_IDS["study_blocks_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        # Cluster layout: each block = 3 cols x 2 rows of sub-cards
        # Blocks stacked vertically below section header
        BASE_X, BASE_Y = self._section_xy("study_blocks")
        BASE_Y += CARD_START_DY + 20

        _SLOT_COLORS = {
            "problem":     "red",
            "diagnostics": "light_blue",
            "topics":      "light_blue",
            "practice":    "light_green",
            "control":     "yellow",
            "next":        "orange",
        }
        # (col, row) in 3x2 grid
        _SLOT_GRID = {
            "problem":     (0, 0),
            "diagnostics": (1, 0),
            "topics":      (2, 0),
            "practice":    (0, 1),
            "control":     (1, 1),
            "next":        (2, 1),
        }
        _SLOT_ENTITY_OFFSET = {
            "problem": 0, "diagnostics": 1, "topics": 2,
            "practice": 3, "control": 4, "next": 5,
        }

        for block_i, b in enumerate(blocks[:MAX_PROBLEMS]):
            block_base_y = BASE_Y + block_i * (CLUSTER_ROW_H * 2 + CLUSTER_GAP_Y * 2 + CLUSTER_BLOCK_GAP)

            # Build plan via builder (no raw transcript)
            try:
                plan = build_plan(b)
                miro_cards = plan.get("miro_cards", [])
            except Exception:
                logger.warning("build_plan failed for block %s, using fallback", b.id)
                miro_cards = [
                    {"slot": "problem",     "content": f"\u041f\u0420\u041e\u0411\u041b\u0415\u041c\u0410\n{b.title}\n\n\u0421\u043b\u0430\u0431\u043e\u0435 \u043c\u0435\u0441\u0442\u043e:\n\u043d\u0443\u0436\u043d\u0430 \u0434\u0438\u0430\u0433\u043d\u043e\u0441\u0442\u0438\u043a\u0430 \u0442\u0438\u043f\u0430 \u043e\u0448\u0438\u0431\u043a\u0438."},
                    {"slot": "diagnostics", "content": "\u0414\u0418\u0410\u0413\u041d\u041e\u0421\u0422\u0418\u041a\u0410\n1. \u041e\u0442\u043a\u0440\u044b\u0442\u044c 3-5 \u0437\u0430\u0434\u0430\u043d\u0438\u0439 \u043f\u043e \u044d\u0442\u043e\u0439 \u0442\u0435\u043c\u0435.\n2. \u0412\u044b\u043f\u0438\u0441\u0430\u0442\u044c \u043e\u0448\u0438\u0431\u043a\u0438.\n3. \u041e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c: \u0442\u0435\u043e\u0440\u0438\u044f, \u0430\u043b\u0433\u043e\u0440\u0438\u0442\u043c \u0438\u043b\u0438 \u043d\u0435\u0432\u043d\u0438\u043c\u0430\u0442\u0435\u043b\u044c\u043d\u043e\u0441\u0442\u044c?"},
                    {"slot": "topics",      "content": "\u0422\u0415\u041c\u042b\n1. \u0418\u0437\u0443\u0447\u0438\u0442\u044c \u0441\u043f\u0435\u0446\u0438\u0444\u0438\u043a\u0430\u0446\u0438\u044e \u0415\u0413\u042d.\n2. \u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u044c \u0442\u0435\u043e\u0440\u0435\u0442\u0438\u0447\u0435\u0441\u043a\u0443\u044e \u0431\u0430\u0437\u0443.\n3. \u041d\u0430\u0439\u0442\u0438 \u043a\u0440\u0438\u0442\u0435\u0440\u0438\u0438 \u043e\u0446\u0435\u043d\u043a\u0438."},
                    {"slot": "practice",    "content": "\u041f\u0420\u0410\u041a\u0422\u0418\u041a\u0410\n1. \u0420\u0435\u0448\u0438\u0442\u044c 5 \u0442\u0438\u043f\u043e\u0432\u044b\u0445 \u0437\u0430\u0434\u0430\u043d\u0438\u0439.\n2. \u0420\u0430\u0437\u043e\u0431\u0440\u0430\u0442\u044c \u043e\u0448\u0438\u0431\u043a\u0438.\n3. \u041f\u043e\u0432\u0442\u043e\u0440 \u0447\u0435\u0440\u0435\u0437 2 \u0434\u043d\u044f."},
                    {"slot": "control",     "content": "\u041a\u041e\u041d\u0422\u0420\u041e\u041b\u042c\n\u041a\u0440\u0438\u0442\u0435\u0440\u0438\u0439 \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u044f:\n• \u0427\u0435\u0440\u0435\u0437 3 \u0434\u043d\u044f: \u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u0441\u043d\u043e\u0432\u0430.\n• \u0427\u0435\u0440\u0435\u0437 \u043d\u0435\u0434\u0435\u043b\u044e: \u043c\u0438\u043d\u0438-\u0442\u0435\u0441\u0442."},
                    {"slot": "next",        "content": f"\u0421\u041b\u0415\u0414\u0423\u042e\u0429\u0418\u0419 \u0428\u0410\u0413\n\n\u0421\u0435\u0433\u043e\u0434\u043d\u044f:\n\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u043e\u0434\u043d\u043e \u0442\u0438\u043f\u043e\u0432\u043e\u0435 \u0437\u0430\u0434\u0430\u043d\u0438\u0435 \u0438 \u0432\u044b\u043f\u0438\u0441\u0430\u0442\u044c \u0430\u043b\u0433\u043e\u0440\u0438\u0442\u043c: {b.title[:40]}"},
                ]

            for card in miro_cards:
                slot = card.get("slot", "")
                if slot not in _SLOT_GRID:
                    continue
                col, row = _SLOT_GRID[slot]
                sub_x = BASE_X + col * (CLUSTER_COL_W + CLUSTER_GAP_X)
                sub_y = block_base_y + row * (CLUSTER_ROW_H + CLUSTER_GAP_Y)

                color = _safe_sticky_color(_SLOT_COLORS.get(slot, "light_gray"))
                entity_id = b.id * 10 + _SLOT_ENTITY_OFFSET.get(slot, 0)
                entity_type = "problem_block_card"

                mp = await self._get_mapping(session, user_id, entity_type, entity_id)
                new_id, _ = await self._create_or_update_sticky(
                    mp.item_id, _safe_content(card.get("content", "—"), 500),
                    sub_x, sub_y, color, entity_type, entity_id, stats,
                )
                await self._save_mapping(mp, new_id or mp.item_id, sub_x, sub_y)
                stats["cards"] += 1

    # ── Legacy placeholder (kept for DB mapping compatibility) ────────────────

    async def _render_future_sections(self, session, user_id: int, time_service, stats: dict) -> None:
        """Legacy stub — not called from sync_all. Kept for mapping key compatibility."""
        pass


    # ── Section: ПЛАН ПО ПРЕДМЕТАМ ───────────────────────────────────────────

    async def _render_subject_plan_section(
        self, session, user_id: int, time_service, stats: dict
    ) -> None:
        """
        Aggregated per-subject plan card.
        For each subject with active exam or blocks: date, blocks, next action.
        Entity type: subject_plan, entity_id: 6000 + abs(hash(sk)) % 900.
        """
        from bot.services.study_problem_templates import _extract_subject_key

        now = time_service.now()
        today = now.date()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "subject_plan", t_str, stats)

        exams = await get_active_exam_dates(session, user_id)
        schedule = await get_active_study_schedule(session, user_id)
        blocks = await get_problem_blocks_by_categories(session, user_id, ["exam", "study"], now)

        if not exams and not blocks:
            x, y = self._card_xy("subject_plan", 0)
            mp = await self._get_mapping(session, user_id, "miro_subj_plan_empty",
                                         _SECTION_ENTITY_IDS["subject_plan_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id,
                "План по предметам будет здесь после добавления экзаменов и блоков.",
                x, y, "light_gray", "miro_subj_plan_empty",
                _SECTION_ENTITY_IDS["subject_plan_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        # Build subject map
        subject_map: dict[str, dict] = {}
        for e in sorted(exams, key=lambda x: x.exam_date):
            sk = _extract_subject_key(e.subject, e.subject, "")
            if sk not in subject_map:
                subject_map[sk] = {"subject": e.subject, "exam": e, "blocks": [], "sessions": [], "next_action": ""}

        for b in blocks:
            subject_raw = getattr(b, "subject", "") or b.title
            sk = _extract_subject_key(subject_raw, b.title, b.problem_text or "")
            if sk not in subject_map:
                subject_map[sk] = {"subject": subject_raw, "exam": None, "blocks": [], "sessions": [], "next_action": ""}
            subject_map[sk]["blocks"].append(b)
            if b.next_action and not subject_map[sk]["next_action"]:
                subject_map[sk]["next_action"] = b.next_action[:100]

        for s in schedule:
            sk = _extract_subject_key(s.subject or "", s.subject or "", "")
            if sk in subject_map:
                subject_map[sk]["sessions"].append(s)

        _WDAY = {"mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
                 "fri": "Пт", "sat": "Сб", "sun": "Вс"}

        for card_i, (sk, info) in enumerate(subject_map.items()):
            lines = [f"ПЛАН\n{info['subject']}\n"]
            if info["exam"]:
                e = info["exam"]
                days_left = (e.exam_date - today).days
                if days_left >= 0:
                    lines.append(f"• экзамен: {e.exam_date.strftime('%d.%m')} ({days_left} дн.)")
                else:
                    lines.append(f"• экзамен: {e.exam_date.strftime('%d.%m')} (прошёл)")
            if info["blocks"]:
                block_titles = ", ".join(b.title[:35] for b in info["blocks"][:3])
                lines.append(f"• блоки: {block_titles}")
            if info["sessions"]:
                sess_strs = []
                for s in info["sessions"][:2]:
                    wd = _WDAY.get(s.weekday or "", s.weekday or "")
                    sess_strs.append(f"{wd} {s.time_str or ''}")
                lines.append(f"• занятия: {', '.join(sess_strs)}")
            if info["next_action"]:
                lines.append(f"• действие: {info['next_action']}")
            elif info["blocks"]:
                lines.append("• действие: отработать слабые места.")

            content = "\n".join(lines)
            entity_id = 6000 + (abs(hash(sk)) % 900)
            entity_type = "subject_plan"

            x, y = self._card_xy("subject_plan", card_i)
            mp = await self._get_mapping(session, user_id, entity_type, entity_id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, _safe_content(content, 500),
                x, y, "violet", entity_type, entity_id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: СЛЕДУЮЩИЕ 72 ЧАСА ────────────────────────────────────────────

    async def _render_next_72h_section(
        self, session, user_id: int, time_service, stats: dict
    ) -> None:
        """
        Next 72 hours summary: upcoming sessions, urgent exams, due review blocks, reminders.
        Single card (stable entity_id = next_72h_card). Always PATCHed, never duplicated.
        """
        from datetime import timedelta

        now = time_service.now()
        today = now.date()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "next_72h", t_str, stats)

        horizon = now + timedelta(hours=72)

        schedule = await get_active_study_schedule(session, user_id)
        exams = await get_active_exam_dates(session, user_id)
        blocks = await get_problem_blocks_by_categories(session, user_id, ["exam", "study"], now)
        reminders = await get_active_reminders(session, user_id)

        lines = ["СЛЕДУЮЩИЕ 72 ЧАСА\n"]

        # 1. Upcoming study sessions (next 3 days by weekday)
        _WDAY_STR = {"mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
                     "fri": "Пт", "sat": "Сб", "sun": "Вс"}
        _WDAY_IDX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        today_wd = today.weekday()  # 0 = Monday
        upcoming_sessions = []
        for s in schedule:
            wd_idx = _WDAY_IDX.get(s.weekday or "", -1)
            if wd_idx < 0:
                continue
            days_until = (wd_idx - today_wd) % 7
            if days_until <= 3:
                upcoming_sessions.append((days_until, s))
        upcoming_sessions.sort(key=lambda x: x[0])
        if upcoming_sessions:
            lines.append("Занятия:")
            for delta_days, s in upcoming_sessions[:4]:
                wd_str = _WDAY_STR.get(s.weekday or "", s.weekday or "")
                tutor = f" — {s.tutor_name}" if s.tutor_name else ""
                lines.append(f"  {wd_str} {s.time_str or ''} {s.subject}{tutor}")

        # 2. Urgent exams ≤14 days
        urgent_exams = [e for e in exams if 0 <= (e.exam_date - today).days <= 14]
        if urgent_exams:
            lines.append("\nЭкзамены:")
            for e in sorted(urgent_exams, key=lambda x: x.exam_date):
                days_left = (e.exam_date - today).days
                lines.append(f"  {e.subject}: {days_left} дн. до экзамена")

        # 3. Problem blocks due for review
        due_blocks = [b for b in blocks if b.next_review_at and b.next_review_at <= horizon]
        if due_blocks:
            lines.append("\nБлоки:")
            for b in sorted(due_blocks, key=lambda x: x.next_review_at)[:3]:
                lines.append(f"  {b.title[:50]} — повторить")

        # 4. Upcoming reminders
        due_reminders = [r for r in reminders if r.remind_at <= horizon]
        if due_reminders:
            lines.append("\nНапоминания:")
            for r in sorted(due_reminders, key=lambda x: x.remind_at)[:3]:
                lines.append(f"  {r.remind_at.strftime('%d.%m %H:%M')} — {r.text[:40]}")

        if len(lines) <= 1:
            lines.append("Ближайших событий нет.")

        content = "\n".join(lines)
        x, y = self._card_xy("next_72h", 0)
        mp = await self._get_mapping(session, user_id, "miro_next72h_card", _SECTION_ENTITY_IDS["next_72h_card"])
        new_id, _ = await self._create_or_update_sticky(
            mp.item_id, _safe_content(content, 800),
            x, y, "light_blue", "miro_next72h_card", _SECTION_ENTITY_IDS["next_72h_card"], stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        stats["cards"] += 1

    # ── Public: sync_all ──────────────────────────────────────────────────────


    async def sync_all(self, user_id: int, session, time_service, cfg, next_step_service=None) -> dict:
        stats = {"sections": 0, "cards": 0, "created": 0, "updated": 0, "errors": 0,
                 "exams": 0, "schedule_items": 0, "study_blocks": 0}
        now = time_service.now()

        # Pre-count study data for report
        try:
            exams_list = await get_active_exam_dates(session, user_id)
            schedule_list = await get_active_study_schedule(session, user_id)
            study_blocks_list = await get_problem_blocks_by_categories(session, user_id, ["exam", "study"], now)
            stats["exams"] = len(exams_list)
            stats["schedule_items"] = len(schedule_list)
            stats["study_blocks"] = len(study_blocks_list)
        except Exception:
            pass

        failed_sections = []

        async def _safe_section(name, coro):
            try:
                await coro
            except Exception as _exc:
                logger.exception("Miro section FAILED [%s]: %s", name, _exc)
                stats["errors"] += 1
                failed_sections.append(name)

        await _safe_section("board_header",
            self._render_board_header(session, user_id, now, stats))
        await _safe_section("today",
            self._render_today_section(session, user_id, time_service, stats))
        await _safe_section("next",
            self._render_next_section_safe(session, user_id, time_service, next_step_service, stats))
        await _safe_section("risks",
            self._render_risks_section(session, user_id, time_service, stats))
        await _safe_section("tasks",
            self._render_tasks_section(session, user_id, time_service, stats))
        await _safe_section("reminders",
            self._render_reminders_section(session, user_id, time_service, stats))
        await _safe_section("problems",
            self._render_problems_section(session, user_id, time_service, stats))
        await _safe_section("schedule",
            self._render_schedule_section(session, user_id, time_service, stats))
        await _safe_section("checkins",
            self._render_checkins_section(session, user_id, time_service, cfg, stats))
        await _safe_section("archive",
            self._render_archive_section(session, user_id, time_service, stats))
        await _safe_section("exams",
            self._render_exams_section(session, user_id, time_service, stats))
        await _safe_section("study_schedule",
            self._render_study_schedule_section(session, user_id, time_service, stats))
        await _safe_section("study_blocks",
            self._render_study_blocks_section(session, user_id, time_service, stats))
        await _safe_section("subject_plan",
            self._render_subject_plan_section(session, user_id, time_service, stats))
        await _safe_section("next_72h",
            self._render_next_72h_section(session, user_id, time_service, stats))

        stats["failed_sections"] = failed_sections
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
