"""
MiroService — visual HQ panel for 813Assistant.

Concept:
  Telegram bot = Operator  (receives data, makes decisions, persists entities)
  Miro board   = Illustrator (shows state, never invents entities)

Compact operational layout:

  [TODAY] [NOW] [NEXT] [BLOCKED] [DONE]

Sections are real Miro frames. Work items are real Miro cards with useful
descriptions. TODAY summarizes reminders, schedule and Health without creating
one decorative item per datum. Empty sections stay empty; legacy decorative
bot items are removed by mapping.

Duplicates prevented by stable entity_type + entity_id stored in miro_mappings.
"""

from __future__ import annotations

import json
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
    get_archived_problem_blocks,
    get_archived_tasks,
    get_closed_reminders,
    get_miro_mappings_for_board,
    get_miro_mappings_for_type,
    get_or_create_miro_mapping,
    get_or_create_runtime_state,
    get_problem_blocks_by_categories,
    get_upcoming_overrides,
)
from bot.services.content_quality import is_meaningful_task
from bot.services.datetime_utils import ensure_aware

logger = logging.getLogger(__name__)

# ── Grid constants ─────────────────────────────────────────────────────────────
COL_STRIDE = 2100       # column pitch  (section_w=1800 + gap=300)
ROW_STRIDE = 1700       # row pitch     (section_h=1400 + gap=300)

SHAPE_W = 1800          # section/frame width
SHAPE_H = 100           # small header shape height
FRAME_H = 1400          # section frame height

CARD_START_DY = 160     # first card Y offset from section origin
CARD_STRIDE_DY = 380    # vertical pitch between consecutive cards

MAX_TASKS = 12
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
    "today":     (0, 0, "ПЛАН НА СЕГОДНЯ",     "#7b44d8", "#ffffff"),
    "now":       (1, 0, "СДЕЛАТЬ СЕЙЧАС",      "#d32f2f", "#ffffff"),
    "next":      (2, 0, "ПОСЛЕ ЭТОГО",         "#f6c000", "#1a1a1a"),
    "blocked":   (3, 0, "ПРЕПЯТСТВИЯ",          "#f57c00", "#ffffff"),
    "done":      (4, 0, "ЗАВЕРШЕНО",           "#90a4ae", "#ffffff"),
    "risks":     (2, 1, "РИСКИ",               "#d32f2f", "#ffffff"),
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


def _safe_frame_color(hex_color: str) -> str:
    """Map dashboard colors to the strict Miro FrameStyle whitelist."""
    mapping = {
        "#7b44d8": "#b384bb",
        "#2d9bf0": "#a6ccf5",
        "#d32f2f": "#f16c7f",
        "#f6c000": "#f5d128",
        "#64b5f6": "#a6ccf5",
        "#f57c00": "#ff9d48",
        "#66bb6a": "#93d275",
        "#90a4ae": "#f5f6f8",
        "#b0bec5": "#f5f6f8",
        "#e0e0e0": "#f5f6f8",
        "#1a1a1a": "#000000",
    }
    return mapping.get(hex_color.lower(), "#f5f6f8")

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
    "now_header":            5040,
    "blocked_header":        5041,
    "done_header":           5042,
    "done_card":             5043,
}


class MiroService:

    def __init__(self, token: str, board_id: str, start_x: int, start_y: int):
        self.token = token
        self.board_id = board_id
        self.start_x = start_x
        self.start_y = start_y
        self._frame_ids: dict[str, str] = {}
        self._frame_positions: dict[str, tuple[int, int]] = {}

    def is_configured(self) -> bool:
        return bool(self.token and self.board_id)

    # ── Position helpers ──────────────────────────────────────────────────────

    def _section_xy(self, key: str) -> tuple[int, int]:
        col, row, *_ = SECTIONS[key]
        # Frame coordinates are centre-based. Keeping the left edge at start_x
        # guarantees that bot-created items never spill into the manual zone.
        x = self.start_x + SHAPE_W // 2 + col * COL_STRIDE
        y = self.start_y + FRAME_H // 2 + row * ROW_STRIDE
        return x, y

    def _card_xy(self, section_key: str, card_index: int) -> tuple[int, int]:
        sx, sy = self._section_xy(section_key)
        frame_top = sy - FRAME_H // 2
        return sx, frame_top + CARD_START_DY + card_index * CARD_STRIDE_DY

    def _board_header_xy(self) -> tuple[int, int]:
        return self.start_x + BOARD_HEADER_W // 2, self.start_y + BOARD_HEADER_DY

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
        except httpx.RequestError:
            logger.exception("Miro network error [%s id=%s %s]", entity_type, entity_id, url)
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
            "position": {"x": x, "y": y, "origin": "center"},
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

    async def _delete_item_quietly(self, item_id: str) -> bool:
        """Delete only a known bot-mapped item; 404 means it is already gone."""
        if not item_id:
            return True
        url = f"https://api.miro.com/v2/boards/{self.board_id}/items/{item_id}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.delete(url, headers=self._headers())
            return response.status_code in (200, 204, 404)
        except httpx.RequestError as exc:
            logger.warning("Miro cleanup failed for item %s: %s", item_id, exc)
            return False

    async def _remove_mapped_item(self, mp) -> None:
        if mp.item_id and await self._delete_item_quietly(mp.item_id):
            mp.item_id = ""
            mp.frame_id = ""

    async def _remove_stale_entity_items(
        self, session, user_id: int, entity_type: str, active_ids: set[int]
    ) -> None:
        mappings = await get_miro_mappings_for_type(
            session, user_id, entity_type, self.board_id
        )
        for mapping in mappings:
            if mapping.entity_id not in active_ids:
                await self._remove_mapped_item(mapping)

    async def _remove_deprecated_dashboard_items(self, session, user_id: int) -> None:
        """Keep one compact operational board and remove legacy bot-only clutter."""
        kept_types = {
            "miro_section_header",
            "miro_today_card",
            "task",
            "problem_block",
            "miro_done_card",
            "archived_task",
            "archived_reminder",
            "archived_problem_block",
        }
        kept_headers = {
            _SECTION_ENTITY_IDS["today_header"],
            _SECTION_ENTITY_IDS["now_header"],
            _SECTION_ENTITY_IDS["next_header"],
            _SECTION_ENTITY_IDS["blocked_header"],
            _SECTION_ENTITY_IDS["done_header"],
        }

        mappings = await get_miro_mappings_for_board(session, user_id, self.board_id)
        for mapping in mappings:
            remove = mapping.entity_type not in kept_types
            if mapping.entity_type == "miro_section_header" and mapping.entity_id not in kept_headers:
                remove = True
            if remove:
                await self._remove_mapped_item(mapping)

    async def _create_or_update_frame(
        self,
        item_id: str,
        title: str,
        x: int,
        y: int,
        fill_hex: str,
        entity_id: int,
        stats: dict,
    ) -> tuple[str | None, str]:
        payload = {
            "data": {
                "title": _safe_content(title, 120),
                "format": "custom",
                "type": "freeform",
            },
            "style": {"fillColor": _safe_frame_color(fill_hex)},
            "geometry": {"width": SHAPE_W, "height": FRAME_H},
            "position": {"x": x, "y": y, "origin": "center"},
        }
        base = f"https://api.miro.com/v2/boards/{self.board_id}/frames"
        if item_id:
            _, op = await self._safe_request(
                "patch", f"{base}/{item_id}", payload, "miro_section_frame", entity_id, stats
            )
            if op == "updated":
                stats["updated"] += 1
                return item_id, op

        new_id, op = await self._safe_request(
            "post", base, payload, "miro_section_frame", entity_id, stats
        )
        if op == "created":
            stats["created"] += 1
            if item_id:
                await self._delete_item_quietly(item_id)
                stats["errors"] = max(0, stats["errors"] - 1)
        return new_id, op

    async def _create_or_update_card(
        self,
        item_id: str,
        title: str,
        description: str,
        x: int,
        y: int,
        theme: str,
        entity_type: str,
        entity_id: int,
        stats: dict,
        frame_id: str = "",
    ) -> tuple[str | None, str]:
        position = {"x": x, "y": y, "origin": "center"}
        if frame_id and frame_id in self._frame_positions:
            frame_x, frame_y = self._frame_positions[frame_id]
            position = {
                "x": x - (frame_x - SHAPE_W // 2),
                "y": y - (frame_y - FRAME_H // 2),
                "origin": "center",
                "relativeTo": "parent_top_left",
            }

        payload = {
            "data": {
                "title": _safe_content(title, 255),
                "description": _safe_content(description, 1000),
            },
            "style": {"cardTheme": theme},
            "geometry": {"width": 500},
            "position": position,
        }
        if frame_id:
            payload["parent"] = {"id": frame_id}
        base = f"https://api.miro.com/v2/boards/{self.board_id}/cards"
        if item_id:
            _, op = await self._safe_request(
                "patch", f"{base}/{item_id}", payload, entity_type, entity_id, stats
            )
            if op == "updated":
                stats["updated"] += 1
                return item_id, op

        new_id, op = await self._safe_request("post", base, payload, entity_type, entity_id, stats)
        if op == "created":
            stats["created"] += 1
            if item_id:
                # A mapping from the old dashboard may point to a sticky note.
                # Replace it with a real card and remove only that mapped item.
                await self._delete_item_quietly(item_id)
                stats["errors"] = max(0, stats["errors"] - 1)
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
        _col, _row, label, fill_hex, _text_hex = SECTIONS[section_key]
        x, y = self._section_xy(section_key)
        entity_type = "miro_section_header"
        entity_id = _SECTION_ENTITY_IDS[f"{section_key}_header"]
        mp = await self._get_mapping(session, user_id, entity_type, entity_id)
        title = f"{label} · обновлено {updated_at}"
        new_id, _op = await self._create_or_update_frame(
            mp.item_id, title, x, y, fill_hex, entity_id, stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        mp.frame_id = new_id or mp.item_id
        if mp.frame_id:
            self._frame_ids[section_key] = mp.frame_id
            self._frame_positions[mp.frame_id] = (x, y)
        stats["sections"] += 1

    # ── Board header ──────────────────────────────────────────────────────────

    async def _render_board_header(self, session, user_id: int, now: datetime, stats: dict) -> None:
        x, y = self._board_header_xy()
        entity_type = "miro_board_header"
        entity_id = _SECTION_ENTITY_IDS["board_header"]
        mp = await self._get_mapping(session, user_id, entity_type, entity_id)
        content = f"  813ASSISTANT / AI-ШТАБ    {now.strftime('%Y-%m-%d %H:%M')}"
        new_id, _op = await self._create_or_update_shape(
            mp.item_id, content, x, y, BOARD_HEADER_W, 130,
            "#1a1a1a", "#ffffff", entity_type, entity_id, stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)

    # ── Section: TODAY ────────────────────────────────────────────────────────

    async def _render_today_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "today", t_str, stats)

        tasks = [task for task in await get_active_tasks(session, user_id) if is_meaningful_task(task)]
        reminders = await get_active_reminders(session, user_id)
        overrides = await get_upcoming_overrides(session, user_id, now.date())
        state = await get_or_create_runtime_state(session, user_id)

        mode = "обычный"
        if any(str(o.date) == str(now.date()) and o.mode == "rest_day" for o in overrides):
            mode = "отдых"
        if state.quiet_until and ensure_aware(state.quiet_until, now.tzinfo) > now:
            mode = "тихий"

        risks = []
        if any(t.deadline and ensure_aware(t.deadline, now.tzinfo) < now for t in tasks):
            risks.append("просрочка")
        if mode == "тихий":
            risks.append("тихий режим")

        now_tasks = [task for task in tasks if task.workflow_state == "now"][:3]
        inbox_count = sum(1 for task in tasks if task.planning_state == "inbox")
        lines = [f"TODAY · {now.strftime('%d.%m.%Y')}", f"Режим: {mode}", "", "Главное:"]
        lines.extend(
            f"{index}. {task.title} — {task.next_action or task.title}"
            for index, task in enumerate(now_tasks, 1)
        )
        if not now_tasks:
            lines.append("— нет выбранных задач")
        if inbox_count:
            lines += ["", f"Входящие требуют уточнения: {inbox_count}"]
        if reminders:
            lines += ["", "Ближайшие напоминания:"]
            lines.extend(
                f"• {item.remind_at.strftime('%H:%M')} {item.text}"
                for item in reminders[:3]
            )
        try:
            from sqlalchemy import select

            from bot.database.models import HealthSnapshot

            result = await session.execute(
                select(HealthSnapshot)
                .where(HealthSnapshot.user_id == user_id, HealthSnapshot.date == now.date())
                .limit(1)
            )
            health = result.scalar_one_or_none()
            if health:
                lines += ["", f"Шаги: {health.steps} / {health.step_goal}"]
        except Exception:
            logger.exception("Miro Today health summary failed")
        try:
            from datetime import timedelta

            from sqlalchemy import func, select

            from bot.database.models import Task

            week_result = await session.execute(
                select(func.count(Task.id)).where(
                    Task.user_id == user_id,
                    Task.status.in_(["done", "archived"]),
                    Task.completed_at >= now - timedelta(days=7),
                )
            )
            completed_week = int(week_result.scalar_one() or 0)
            projects = len({task.project for task in tasks if task.project})
            lines += ["", f"Неделя: завершено {completed_week} · активных проектов {projects}"]
        except Exception:
            logger.exception("Miro Today weekly summary failed")
        lines += ["", f"Риски: {', '.join(risks) if risks else 'нет'}"]
        content = "\n".join(lines)
        x, y = self._card_xy("today", 0)
        mp = await self._get_mapping(session, user_id, "miro_today_card", _SECTION_ENTITY_IDS["today_card"])
        new_id, _ = await self._create_or_update_card(
            mp.item_id,
            f"Сегодня · {now.strftime('%d.%m.%Y')}",
            content,
            x,
            y,
            "#7b44d8",
            "miro_today_card",
            _SECTION_ENTITY_IDS["today_card"],
            stats,
            self._frame_ids.get("today", ""),
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
        except Exception:
            logger.exception("NextStepService.build_next_step failed")
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
        overdue = [t for t in tasks if t.deadline and ensure_aware(t.deadline, now.tzinfo) < now]
        if overdue:
            risks.append(f"просрочка: {len(overdue)} задач")
            for t in overdue[:3]:
                risks.append(f"  — {t.title[:50]}")
        if state.quiet_until and ensure_aware(state.quiet_until, now.tzinfo) > now:
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

        tasks = await get_active_tasks(session, user_id)
        stats["tasks"] = len(tasks)
        frame_id = self._frame_ids.get("tasks", "")
        empty_mapping = await self._get_mapping(
            session, user_id, "miro_tasks_empty", _SECTION_ENTITY_IDS["tasks_header"] + 50
        )
        await self._remove_mapped_item(empty_mapping)

        if not tasks:
            await self._remove_stale_entity_items(session, user_id, "task", set())
            return

        groups = {
            "high": [t for t in tasks if t.priority in {"urgent", "high"}],
            "medium": [t for t in tasks if t.priority == "medium"],
            "low": [t for t in tasks if t.priority not in {"urgent", "high", "medium"}],
        }
        visible_task_ids = {task.id for items in groups.values() for task in items[:4]}
        await self._remove_stale_entity_items(
            session, user_id, "task", visible_task_ids
        )
        lane_meta = {
            "high": ("СЕЙЧАС · HIGH", "#d32f2f", -580, 5101),
            "medium": ("ДАЛЬШЕ · MEDIUM", "#f6c000", 0, 5102),
            "low": ("ПОТОМ · LOW", "#90a4ae", 580, 5103),
        }
        section_x, section_y = self._section_xy("tasks")
        frame_top = section_y - FRAME_H // 2

        for key, items in groups.items():
            lane_title, theme, dx, lane_id = lane_meta[key]
            lane_mapping = await self._get_mapping(session, user_id, "miro_task_lane", lane_id)
            lane_description = f"{len(items)} задач" if len(items) != 1 else "1 задача"
            lane_x, lane_y = section_x + dx, frame_top + 170
            lane_item_id, _ = await self._create_or_update_card(
                lane_mapping.item_id,
                lane_title,
                lane_description,
                lane_x,
                lane_y,
                theme,
                "miro_task_lane",
                lane_id,
                stats,
                frame_id,
            )
            await self._save_mapping(lane_mapping, lane_item_id or lane_mapping.item_id, lane_x, lane_y)
            lane_mapping.frame_id = frame_id
            stats["cards"] += 1

            for row, task in enumerate(items[:4]):
                deadline = task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "без дедлайна"
                details = [f"Приоритет: {task.priority}", f"Дедлайн: {deadline}"]
                if task.deadline and ensure_aware(task.deadline, now.tzinfo) < now:
                    details.append("ПРОСРОЧЕНО")
                if task.description:
                    details += ["", task.description]
                if task.project:
                    details.append(f"Проект: {task.project}")
                elif task.category:
                    details.append(f"Категория: {task.category}")
                x, y = lane_x, frame_top + 410 + row * 235
                mp = await self._get_mapping(session, user_id, "task", task.id)
                new_id, _ = await self._create_or_update_card(
                    mp.item_id,
                    task.title,
                    "\n".join(details),
                    x,
                    y,
                    theme,
                    "task",
                    task.id,
                    stats,
                    frame_id,
                )
                await self._save_mapping(mp, new_id or mp.item_id, x, y)
                mp.frame_id = frame_id
                stats["cards"] += 1

        overflow_mapping = await self._get_mapping(
            session, user_id, "miro_tasks_overflow", _SECTION_ENTITY_IDS["tasks_header"] + 99
        )
        await self._remove_mapped_item(overflow_mapping)

    # ── Section: REMINDERS ────────────────────────────────────────────────────

    async def _render_reminders_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "reminders", now.strftime("%H:%M"), stats)

        reminders = sorted(
            await get_active_reminders(session, user_id),
            key=lambda r: (
                0 if ensure_aware(r.remind_at, now.tzinfo) <= now else 1,
                ensure_aware(r.remind_at, now.tzinfo),
            )
        )
        stats["reminders"] = len(reminders)
        visible_reminders = reminders[:MAX_REMINDERS]
        await self._remove_stale_entity_items(
            session, user_id, "reminder", {reminder.id for reminder in visible_reminders}
        )
        empty_mapping = await self._get_mapping(
            session, user_id, "miro_rem_empty", _SECTION_ENTITY_IDS["reminders_header"] + 50
        )
        await self._remove_mapped_item(empty_mapping)

        if not reminders:
            return

        for i, r in enumerate(visible_reminders):
            overdue = ensure_aware(r.remind_at, now.tzinfo) <= now
            theme = "#d32f2f" if overdue else "#2d9bf0"
            when = r.remind_at.strftime("%Y-%m-%d %H:%M")
            details = f"Когда: {when}\nСтатус: active"
            if overdue:
                details += "\nПРОСРОЧЕНО"
            x, y = self._card_xy("reminders", i)
            mp = await self._get_mapping(session, user_id, "reminder", r.id)
            new_id, _ = await self._create_or_update_card(
                mp.item_id, r.text, details, x, y, theme, "reminder", r.id, stats,
                self._frame_ids.get("reminders", ""),
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            mp.frame_id = self._frame_ids.get("reminders", "")
            stats["cards"] += 1

    # ── Section: PROBLEMS ─────────────────────────────────────────────────────

    async def _render_problems_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "problems", now.strftime("%H:%M"), stats)

        blocks = await get_active_problem_blocks(session, user_id, now)
        stats["problems"] = len(blocks)
        visible_blocks = blocks[:MAX_PROBLEMS]
        await self._remove_stale_entity_items(
            session, user_id, "problem_block", {block.id for block in visible_blocks}
        )
        empty_mapping = await self._get_mapping(
            session, user_id, "miro_prob_empty", _SECTION_ENTITY_IDS["problems_header"] + 50
        )
        await self._remove_mapped_item(empty_mapping)

        if not blocks:
            return

        for i, b in enumerate(visible_blocks):
            theme = "#d32f2f" if (b.priority in {"urgent", "high"} or b.pressure_level == "hard") else "#f57c00"
            details = [
                f"Категория: {b.category}",
                f"Давление: {b.pressure_level}",
                "",
                f"Следующий шаг: {b.next_action or 'уточнить'}",
            ]
            if b.deadline:
                details.append(f"Дедлайн: {b.deadline.strftime('%d.%m.%Y %H:%M')}")
            x, y = self._card_xy("problems", i)
            mp = await self._get_mapping(session, user_id, "problem_block", b.id)
            new_id, _ = await self._create_or_update_card(
                mp.item_id, b.title, "\n".join(details), x, y, theme,
                "problem_block", b.id, stats, self._frame_ids.get("problems", ""),
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            mp.frame_id = self._frame_ids.get("problems", "")
            stats["cards"] += 1

    # ── Section: SCHEDULE ─────────────────────────────────────────────────────

    async def _render_schedule_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "schedule", now.strftime("%H:%M"), stats)

        overrides = await get_upcoming_overrides(session, user_id, now.date())
        stats["schedule"] = len(overrides)
        visible_overrides = overrides[:MAX_SCHEDULE]
        await self._remove_stale_entity_items(
            session, user_id, "schedule_override", {override.id for override in visible_overrides}
        )
        empty_mapping = await self._get_mapping(
            session, user_id, "miro_sched_empty", _SECTION_ENTITY_IDS["schedule_header"] + 50
        )
        await self._remove_mapped_item(empty_mapping)

        if not overrides:
            return

        for i, o in enumerate(visible_overrides):
            theme = "#66bb6a" if o.mode == "rest_day" else "#2d9bf0"
            description = f"Режим: {o.mode}"
            if o.description:
                description += f"\n\n{o.description}"
            x, y = self._card_xy("schedule", i)
            mp = await self._get_mapping(session, user_id, "schedule_override", o.id)
            new_id, _ = await self._create_or_update_card(
                mp.item_id, str(o.date), description, x, y, theme,
                "schedule_override", o.id, stats, self._frame_ids.get("schedule", ""),
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            mp.frame_id = self._frame_ids.get("schedule", "")
            stats["cards"] += 1

    # ── Section: CHECK-INS ────────────────────────────────────────────────────

    async def _render_checkins_section(self, session, user_id: int, time_service, cfg, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "checkins", now.strftime("%H:%M"), stats)

        state = await get_or_create_runtime_state(session, user_id)
        quiet = (
            state.quiet_until.strftime("%H:%M")
            if state.quiet_until and ensure_aware(state.quiet_until, now.tzinfo) > now
            else "нет"
        )
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
        closed_reminders = await get_closed_reminders(session, user_id)
        done_rem = len(closed_reminders)
        stats["archive"] = len(archived_tasks) + len(archived_blocks) + done_rem

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

        archive_items = []
        archive_items += [
            (t.updated_at, "archived_task", t.id, t.title, f"Тип: задача\nСтатус: {t.status}\nПричина: {t.cleanup_reason or 'выполнено'}")
            for t in archived_tasks
        ]
        archive_items += [
            (r.updated_at, "archived_reminder", r.id, r.text, f"Тип: напоминание\nСтатус: {r.status}")
            for r in closed_reminders
        ]
        archive_items += [
            (b.updated_at, "archived_problem_block", b.id, b.title, f"Тип: блок\nСтатус: {b.status}\nПричина: {b.archive_reason or 'закрыт'}")
            for b in archived_blocks
        ]
        archive_items.sort(key=lambda item: item[0] or now, reverse=True)
        displayed = archive_items[:10]

        for mapping_type in ("archived_task", "archived_reminder", "archived_problem_block"):
            visible_ids = {item[2] for item in displayed if item[1] == mapping_type}
            await self._remove_stale_entity_items(session, user_id, mapping_type, visible_ids)

        for i, (_, mapping_type, entity_id, title, description) in enumerate(displayed):
            section_x, section_y = self._section_xy("archive")
            frame_top = section_y - FRAME_H // 2
            column, row = divmod(i, 5)
            x = section_x + (-420 if column == 0 else 420)
            y = frame_top + 390 + row * 210
            mp = await self._get_mapping(session, user_id, mapping_type, entity_id)
            new_id, _ = await self._create_or_update_card(
                mp.item_id, title, description, x, y, "#90a4ae",
                mapping_type, entity_id, stats, self._frame_ids.get("archive", ""),
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            mp.frame_id = self._frame_ids.get("archive", "")
            stats["cards"] += 1

    # ── Section: ЭКЗАМЕНЫ ────────────────────────────────────────────────────

    async def _render_exams_section(self, session, user_id: int, time_service, stats: dict) -> None:
        """
        Informative exam cards: subject, date, days left, status, subject-specific focus.
        Deduplicates exams by normalized subject key (keeps closest future date).
        """
        from bot.services.study_problem_plan_builder import (
            _days_status,
            exam_focus_text,
        )
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
            for _delta_days, s in upcoming_sessions[:4]:
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
        due_blocks = [
            b for b in blocks
            if b.next_review_at and ensure_aware(b.next_review_at, now.tzinfo) <= horizon
        ]
        if due_blocks:
            lines.append("\nБлоки:")
            for b in sorted(due_blocks, key=lambda x: x.next_review_at)[:3]:
                lines.append(f"  {b.title[:50]} — повторить")

        # 4. Upcoming reminders
        due_reminders = [
            r for r in reminders
            if ensure_aware(r.remind_at, now.tzinfo) <= horizon
        ]
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

    # ── Compact operational workflow ─────────────────────────────────────────

    async def _render_task_workflow_board(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        for section_key in ("now", "next", "blocked"):
            await self._render_section_header(
                session, user_id, section_key, now.strftime("%H:%M"), stats
            )

        tasks = await get_active_tasks(session, user_id)
        stats["tasks"] = len(tasks)
        groups = {
            "now": [task for task in tasks if task.workflow_state == "now"],
            "next": [task for task in tasks if task.workflow_state == "next"],
            "blocked": [task for task in tasks if task.workflow_state == "blocked"],
        }
        visible_tasks = [task for section in groups.values() for task in section[:5]]
        await self._remove_stale_entity_items(
            session, user_id, "task", {task.id for task in visible_tasks}
        )

        themes = {"now": "#d32f2f", "next": "#f6c000", "blocked": "#f57c00"}
        for section_key, items in groups.items():
            frame_id = self._frame_ids.get(section_key, "")
            for index, task in enumerate(items[:5]):
                details = [
                    f"Результат: {task.outcome or task.title}",
                    f"Начать с: {task.next_action or task.title}",
                    f"Нужно времени: {task.estimated_minutes} мин",
                ]
                if task.scheduled_start and task.scheduled_end:
                    details.append(
                        f"В плане: {task.scheduled_start.strftime('%H:%M')}–"
                        f"{task.scheduled_end.strftime('%H:%M')}"
                    )
                if task.deadline:
                    details.append(f"Срок: {task.deadline.strftime('%d.%m.%Y %H:%M')}")
                if task.project:
                    details.append(f"Проект: {task.project}")
                if task.dependencies_json and task.dependencies_json != "[]":
                    try:
                        dependencies = json.loads(task.dependencies_json)
                    except Exception:
                        dependencies = []
                    if dependencies:
                        details.append(f"Зависит от: {', '.join(dependencies[:3])}")
                if task.blocked_reason:
                    details.append(f"Препятствие: {task.blocked_reason}")
                section_x, section_y = self._section_xy(section_key)
                x = section_x
                y = section_y - FRAME_H // 2 + 190 + index * 235
                mapping = await self._get_mapping(session, user_id, "task", task.id)
                item_id, _ = await self._create_or_update_card(
                    mapping.item_id,
                    task.title,
                    "\n".join(details),
                    x,
                    y,
                    themes[section_key],
                    "task",
                    task.id,
                    stats,
                    frame_id,
                )
                await self._save_mapping(mapping, item_id or mapping.item_id, x, y)
                mapping.frame_id = frame_id
                stats["cards"] += 1

        blocks = await get_active_problem_blocks(session, user_id, now)
        stats["problems"] = len(blocks)
        visible_blocks = blocks[: max(0, 5 - len(groups["blocked"][:5]))]
        await self._remove_stale_entity_items(
            session, user_id, "problem_block", {block.id for block in visible_blocks}
        )
        start_index = len(groups["blocked"][:5])
        for offset, block in enumerate(visible_blocks):
            section_x, section_y = self._section_xy("blocked")
            x = section_x
            y = section_y - FRAME_H // 2 + 190 + (start_index + offset) * 235
            description = (
                f"Препятствие: {block.problem_text or block.title}\n"
                f"Следующее действие: {block.next_action or 'уточнить'}"
            )
            mapping = await self._get_mapping(session, user_id, "problem_block", block.id)
            item_id, _ = await self._create_or_update_card(
                mapping.item_id,
                block.title,
                description,
                x,
                y,
                "#f57c00",
                "problem_block",
                block.id,
                stats,
                self._frame_ids.get("blocked", ""),
            )
            await self._save_mapping(mapping, item_id or mapping.item_id, x, y)
            mapping.frame_id = self._frame_ids.get("blocked", "")
            stats["cards"] += 1

    async def _render_done_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        await self._render_section_header(session, user_id, "done", now.strftime("%H:%M"), stats)
        tasks = await get_archived_tasks(session, user_id)
        reminders = await get_closed_reminders(session, user_id)
        blocks = await get_archived_problem_blocks(session, user_id)
        stats["archive"] = len(tasks) + len(reminders) + len(blocks)

        summary_mapping = await self._get_mapping(
            session, user_id, "miro_done_card", _SECTION_ENTITY_IDS["done_card"]
        )
        summary = f"Задачи: {len(tasks)} · напоминания: {len(reminders)} · блоки: {len(blocks)}"
        summary_x, summary_y = self._card_xy("done", 0)
        summary_id, _ = await self._create_or_update_card(
            summary_mapping.item_id,
            "Завершено",
            summary,
            summary_x,
            summary_y,
            "#90a4ae",
            "miro_done_card",
            _SECTION_ENTITY_IDS["done_card"],
            stats,
            self._frame_ids.get("done", ""),
        )
        await self._save_mapping(
            summary_mapping, summary_id or summary_mapping.item_id, summary_x, summary_y
        )
        stats["cards"] += 1

        items = [
            (task.updated_at, "archived_task", task.id, task.title, "Выполненная задача")
            for task in tasks
        ]
        items += [
            (reminder.updated_at, "archived_reminder", reminder.id, reminder.text, "Закрытое напоминание")
            for reminder in reminders
        ]
        items += [
            (block.updated_at, "archived_problem_block", block.id, block.title, "Закрытое препятствие")
            for block in blocks
        ]
        items.sort(key=lambda item: item[0] or now, reverse=True)
        visible = items[:4]
        for mapping_type in ("archived_task", "archived_reminder", "archived_problem_block"):
            await self._remove_stale_entity_items(
                session,
                user_id,
                mapping_type,
                {item[2] for item in visible if item[1] == mapping_type},
            )
        for index, (_, mapping_type, entity_id, title, description) in enumerate(visible, 1):
            section_x, section_y = self._section_xy("done")
            x = section_x
            y = section_y - FRAME_H // 2 + 320 + (index - 1) * 235
            mapping = await self._get_mapping(session, user_id, mapping_type, entity_id)
            item_id, _ = await self._create_or_update_card(
                mapping.item_id,
                title,
                description,
                x,
                y,
                "#90a4ae",
                mapping_type,
                entity_id,
                stats,
                self._frame_ids.get("done", ""),
            )
            await self._save_mapping(mapping, item_id or mapping.item_id, x, y)
            mapping.frame_id = self._frame_ids.get("done", "")
            stats["cards"] += 1

    # ── Public: sync_all ──────────────────────────────────────────────────────


    async def sync_all(self, user_id: int, session, time_service, cfg, next_step_service=None) -> dict:
        stats = {"sections": 0, "cards": 0, "created": 0, "updated": 0, "errors": 0,
                 "tasks": 0, "reminders": 0, "problems": 0, "schedule": 0, "archive": 0,
                 "exams": 0, "schedule_items": 0, "study_blocks": 0}
        now = time_service.now()
        self._frame_ids = {}
        self._frame_positions = {}

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
            except Exception:
                logger.exception("Miro section FAILED [%s]", name)
                stats["errors"] += 1
                failed_sections.append(name)

        await _safe_section("cleanup_old_panels",
            self._remove_deprecated_dashboard_items(session, user_id))
        from bot.services.task_prioritization_service import TaskPrioritizationService
        await _safe_section(
            "priority",
            TaskPrioritizationService().refresh(session, user_id, now),
        )
        from bot.services.calendar_service import CalendarService
        await _safe_section(
            "calendar_plan",
            CalendarService(time_service).build_day_plan(session, user_id, now),
        )
        try:
            stats["reminders"] = len(await get_active_reminders(session, user_id))
            stats["schedule"] = len(await get_upcoming_overrides(session, user_id, now.date()))
        except Exception:
            logger.exception("Miro operational counters failed")
        await _safe_section("today",
            self._render_today_section(session, user_id, time_service, stats))
        await _safe_section(
            "workflow",
            self._render_task_workflow_board(session, user_id, time_service, stats),
        )
        await _safe_section(
            "done",
            self._render_done_section(session, user_id, time_service, stats),
        )

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
