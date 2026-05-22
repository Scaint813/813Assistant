#!/usr/bin/env python3
"""
Patch miro_service.py:
1. Add exams / study_schedule / study_blocks as dedicated sections in row 4
2. Add entity IDs for new sections
3. Add _render_exams_section, _render_study_schedule_section, _render_study_blocks_section
4. Remove УЧЁБА from _render_future_sections (it gets its own row now)
5. Call new sections from sync_all
6. Add note to miro_debug about debug vs sync
"""
import re

path = "bot/services/miro_service.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

# ── 1. Extend SECTIONS ────────────────────────────────────────────────────────
OLD_SECTIONS = '''\
    "money":     (0, 3, "ДЕНЬГИ",              "#b0bec5", "#1a1a1a"),
    "orders":    (1, 3, "ЗАКАЗЫ",              "#b0bec5", "#1a1a1a"),
    "study":     (2, 3, "УЧЁБА",              "#b0bec5", "#1a1a1a"),
    "body":      (3, 3, "ТЕЛО",               "#b0bec5", "#1a1a1a"),
    "protocols": (4, 3, "ПРОТОКОЛЫ",           "#b0bec5", "#1a1a1a"),
}'''

NEW_SECTIONS = '''\
    "money":         (0, 3, "ДЕНЬГИ",                  "#b0bec5", "#1a1a1a"),
    "orders":        (1, 3, "ЗАКАЗЫ",                  "#b0bec5", "#1a1a1a"),
    "body":          (2, 3, "ТЕЛО",                    "#b0bec5", "#1a1a1a"),
    "protocols":     (3, 3, "ПРОТОКОЛЫ",               "#b0bec5", "#1a1a1a"),
    # Row 4 — учёба (3 dedicated sections)
    "exams":         (0, 4, "ЭКЗАМЕНЫ / ДЕДЛАЙНЫ",     "#d32f2f", "#ffffff"),
    "study_schedule":(1, 4, "РАСПИСАНИЕ / РЕПЕТИТОРЫ",  "#2d9bf0", "#ffffff"),
    "study_blocks":  (2, 4, "УЧЕБНЫЕ БЛОКИ",            "#f57c00", "#ffffff"),
    # keep study key for legacy mapping header (hidden, offset far right)
    "study":         (5, 4, "УЧЁБА (legacy)",           "#e0e0e0", "#9e9e9e"),
}'''

if OLD_SECTIONS in content:
    content = content.replace(OLD_SECTIONS, NEW_SECTIONS, 1)
    print("OK: SECTIONS extended with exams/study_schedule/study_blocks rows")
else:
    print("WARN: SECTIONS block not found – check miro_service.py manually")

# ── 2. Extend _SECTION_ENTITY_IDS ─────────────────────────────────────────────
OLD_ENTITY_IDS = '''\
    "study_header":     5019,  "study_card":     5020,
    "body_header":      5021,  "body_card":      5022,
    "protocols_header": 5023,  "protocols_card": 5024,
}'''

NEW_ENTITY_IDS = '''\
    "study_header":          5019,  "study_card":          5020,
    "body_header":           5021,  "body_card":           5022,
    "protocols_header":      5023,  "protocols_card":      5024,
    # New study sections (row 4)
    "exams_header":          5025,
    "study_schedule_header": 5026,
    "study_blocks_header":   5027,
    "money_header":          5028,  "money_card":          5029,
    "orders_header":         5030,  "orders_card":         5031,
}'''

if OLD_ENTITY_IDS in content:
    content = content.replace(OLD_ENTITY_IDS, NEW_ENTITY_IDS, 1)
    print("OK: _SECTION_ENTITY_IDS extended")
else:
    print("WARN: _SECTION_ENTITY_IDS block not found")

# ── 3. Inject new section methods before _render_future_sections ───────────────
INJECT_BEFORE = "    # ── Section: FUTURE (placeholders) ───────────────────────────────────────\n"

NEW_METHODS = """\
    # ── Section: ЭКЗАМЕНЫ ────────────────────────────────────────────────────

    async def _render_exams_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        today = now.date()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "exams", t_str, stats)

        exams = await get_active_exam_dates(session, user_id)

        if not exams:
            x, y = self._card_xy("exams", 0)
            mp = await self._get_mapping(session, user_id, "miro_exams_empty", _SECTION_ENTITY_IDS["exams_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "Экзамены не зафиксированы.\\nНапиши: \\"ЕГЭ по обществу 10 июня\\"",
                x, y, "light_gray", "miro_exams_empty", _SECTION_ENTITY_IDS["exams_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        for i, e in enumerate(exams):
            days_left = (e.exam_date - today).days
            if days_left < 0:
                color = "light_gray"
                days_str = "прошло"
            elif days_left <= 7:
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
            content = f"ЭКЗАМЕН\\n{e.subject}\\nДата: {e.exam_date.strftime('%d.%m.%Y')}\\n{days_str}"
            x, y = self._card_xy("exams", i)
            mp = await self._get_mapping(session, user_id, "exam_date", e.id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, color, "exam_date", e.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: РАСПИСАНИЕ / РЕПЕТИТОРЫ ─────────────────────────────────────

    async def _render_study_schedule_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "study_schedule", t_str, stats)

        schedule = await get_active_study_schedule(session, user_id)

        if not schedule:
            x, y = self._card_xy("study_schedule", 0)
            mp = await self._get_mapping(session, user_id, "miro_sched_empty",
                                         _SECTION_ENTITY_IDS["study_schedule_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "Занятия не зафиксированы.\\nНапиши: \\"репетитор по англ по вторникам 18:00\\"",
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
            tutor_line = f"\\n{s.tutor_name}" if s.tutor_name else ""
            content = f"ЗАНЯТИЕ\\n{s.subject}\\n{wd}, {t}{tutor_line}"
            x, y = self._card_xy("study_schedule", i)
            mp = await self._get_mapping(session, user_id, "study_schedule_item", s.id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, "light_green", "study_schedule_item", s.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: УЧЕБНЫЕ БЛОКИ ────────────────────────────────────────────────

    async def _render_study_blocks_section(self, session, user_id: int, time_service, stats: dict) -> None:
        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "study_blocks", t_str, stats)

        blocks = await get_problem_blocks_by_categories(session, user_id, ["exam", "study"], now)

        if not blocks:
            x, y = self._card_xy("study_blocks", 0)
            mp = await self._get_mapping(session, user_id, "miro_study_bl_empty",
                                         _SECTION_ENTITY_IDS["study_blocks_header"] + 50)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, "Учебных блоков нет.",
                x, y, "light_gray", "miro_study_bl_empty",
                _SECTION_ENTITY_IDS["study_blocks_header"] + 50, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1
            return

        for i, b in enumerate(blocks[:MAX_PROBLEMS]):
            color = "red" if b.priority in {"urgent", "high"} else "orange"
            dl = b.deadline.strftime("%d.%m.%Y") if b.deadline else "—"
            next_act = (b.next_action[:60] + "...") if len(b.next_action or "") > 60 else (b.next_action or "—")
            content = f"БЛОК\\n{b.title}\\nДедлайн: {dl}\\nСледующий шаг:\\n{next_act}"
            x, y = self._card_xy("study_blocks", i)
            mp = await self._get_mapping(session, user_id, "problem_block_study", b.id)
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x, y, color, "problem_block_study", b.id, stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x, y)
            stats["cards"] += 1

    # ── Section: FUTURE (placeholders) ───────────────────────────────────────
"""

if INJECT_BEFORE in content:
    content = content.replace(INJECT_BEFORE, NEW_METHODS, 1)
    print("OK: injected _render_exams_section, _render_study_schedule_section, _render_study_blocks_section")
else:
    print("WARN: FUTURE placeholder comment not found")

# ── 4. Update _render_future_sections: remove УЧЁБА block ─────────────────────
OLD_STUDY_BLOCK = '''\
        # ── STUDY: real exam/schedule data ────────────────────────────────────
        await self._render_section_header(session, user_id, "study", t_str, stats)
        x0, y0 = self._card_xy("study", 0)
        if exams or schedule:
            from datetime import date as _date
            today = now.date()
            card_idx = 0
            for e in exams[:4]:
                days_left = (e.exam_date - today).days
                days_str = f"осталось {days_left} дн." if days_left >= 0 else "прошло"
                color = "red" if days_left <= 14 else ("orange" if days_left <= 30 else "light_blue")
                content = f"ЭКЗАМЕН\\n{e.subject}\\nдата: {e.exam_date.strftime('%d.%m.%Y')}\\n{days_str}"
                x, y = self._card_xy("study", card_idx)
                mp = await self._get_mapping(session, user_id, "exam_date", e.id)
                new_id, _ = await self._create_or_update_sticky(
                    mp.item_id, content, x, y, color, "exam_date", e.id, stats,
                )
                await self._save_mapping(mp, new_id or mp.item_id, x, y)
                stats["cards"] += 1
                card_idx += 1
            _WDAY = {"mon": "пн", "tue": "вт", "wed": "ср", "thu": "чт", "fri": "пт", "sat": "сб", "sun": "вс"}
            for s in schedule[:3]:
                wd = _WDAY.get(s.weekday or "", s.weekday or "")
                content = f"ЗАНЯТИЕ\\n{s.subject}\\n{wd}, {s.time_str}"
                if s.tutor_name:
                    content += f"\\n{s.tutor_name}"
                x, y = self._card_xy("study", card_idx)
                mp = await self._get_mapping(session, user_id, "study_schedule_item", s.id)
                new_id, _ = await self._create_or_update_sticky(
                    mp.item_id, content, x, y, "light_green", "study_schedule_item", s.id, stats,
                )
                await self._save_mapping(mp, new_id or mp.item_id, x, y)
                stats["cards"] += 1
                card_idx += 1
        else:
            mp = await self._get_mapping(session, user_id, "miro_study_card", _SECTION_ENTITY_IDS["study_card"])
            content = "УЧЁБА\\n\\nэкзамены не зафиксированы.\\nНапиши: \\"ЕГЭ по обществу 10 июня\\""
            new_id, _ = await self._create_or_update_sticky(
                mp.item_id, content, x0, y0, "light_gray", "miro_study_card",
                _SECTION_ENTITY_IDS["study_card"], stats,
            )
            await self._save_mapping(mp, new_id or mp.item_id, x0, y0)
            stats["cards"] += 1

        # ── Other sections ────────────────────────────────────────────────────
        other_map = {
            "money":     (money_tasks,  "ДЕНЬГИ",     "платежи и расчёты"),
            "orders":    (orders_tasks, "ЗАКАЗЫ",     "клиенты и доставки"),
            "body":      (body_tasks,   "ТЕЛО",       "сон и восстановление"),
            "protocols": (0,            "ПРОТОКОЛЫ",  "сценарии поведения"),
        }'''

NEW_STUDY_BLOCK = '''\
        # ── Other sections (УЧЁБА now has its own dedicated row) ─────────────
        other_map = {
            "money":     (money_tasks,  "ДЕНЬГИ",     "платежи и расчёты"),
            "orders":    (orders_tasks, "ЗАКАЗЫ",     "клиенты и доставки"),
            "body":      (body_tasks,   "ТЕЛО",       "сон и восстановление"),
            "protocols": (0,            "ПРОТОКОЛЫ",  "сценарии поведения"),
        }'''

if OLD_STUDY_BLOCK in content:
    content = content.replace(OLD_STUDY_BLOCK, NEW_STUDY_BLOCK, 1)
    print("OK: removed old УЧЁБА block from _render_future_sections")
else:
    print("WARN: old УЧЁБА block not found – may already be clean")

# ── 5. Remove unused exam/schedule fetch in _render_future_sections ────────────
OLD_FETCH = """\
        tasks = await get_active_tasks(session, user_id)
        blocks = await get_active_problem_blocks(session, user_id, now)
        exams = await get_active_exam_dates(session, user_id)
        schedule = await get_active_study_schedule(session, user_id)

        money_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["оплата", "деньги", "платёж"]))
        orders_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["заказ", "клиент", "доставка"]))
        body_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["сон", "тело", "боль", "перегруз"]))"""

NEW_FETCH = """\
        tasks = await get_active_tasks(session, user_id)
        blocks = await get_active_problem_blocks(session, user_id, now)

        money_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["оплата", "деньги", "платёж"]))
        orders_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["заказ", "клиент", "доставка"]))
        body_tasks = sum(1 for t in tasks if any(kw in t.title.lower() for kw in ["сон", "тело", "боль", "перегруз"]))"""

if OLD_FETCH in content:
    content = content.replace(OLD_FETCH, NEW_FETCH, 1)
    print("OK: removed unused exams/schedule fetch in _render_future_sections")
else:
    print("INFO: fetch block already cleaned or not found")

# ── 6. Add new sections to sync_all ───────────────────────────────────────────
OLD_SYNC_END = """\
        await self._render_archive_section(session, user_id, time_service, stats)
        await self._render_future_sections(session, user_id, time_service, stats)

        return stats"""

NEW_SYNC_END = """\
        await self._render_archive_section(session, user_id, time_service, stats)
        await self._render_future_sections(session, user_id, time_service, stats)
        # ── Study row (exams / schedule / blocks) ──────────────────────────
        try:
            await self._render_exams_section(session, user_id, time_service, stats)
        except Exception as _exc:
            logger.exception("Miro: _render_exams_section failed: %s", _exc)
            stats["errors"] += 1
        try:
            await self._render_study_schedule_section(session, user_id, time_service, stats)
        except Exception as _exc:
            logger.exception("Miro: _render_study_schedule_section failed: %s", _exc)
            stats["errors"] += 1
        try:
            await self._render_study_blocks_section(session, user_id, time_service, stats)
        except Exception as _exc:
            logger.exception("Miro: _render_study_blocks_section failed: %s", _exc)
            stats["errors"] += 1

        return stats"""

if OLD_SYNC_END in content:
    content = content.replace(OLD_SYNC_END, NEW_SYNC_END, 1)
    print("OK: sync_all now calls exam/schedule/study_blocks sections")
else:
    print("WARN: sync_all ending not found")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("Miro patch complete.")
