#!/usr/bin/env python3
"""
Patch miro_service.py:
1. Remove "format": "plain" from shape payload (Miro API 400: Field [data.format] is not supported)
2. Add fallback: if shape POST fails → create sticky_note header instead
3. Wrap ALL sync_all sections (not just study) in try/except
4. Improve sync report: list failed sections by name
"""

path = "bot/services/miro_service.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

# ── 1. Remove "format": "plain" from shape payload ──────────────────────────
OLD_SHAPE_PAYLOAD = '            "data": {"content": _safe_content(content), "format": "plain"},'
NEW_SHAPE_PAYLOAD = '            "data": {"content": _safe_content(content)},'

if OLD_SHAPE_PAYLOAD in content:
    content = content.replace(OLD_SHAPE_PAYLOAD, NEW_SHAPE_PAYLOAD, 1)
    print("OK: removed format:plain from shape payload")
else:
    print("WARN: shape payload format not found")

# ── 2. Add fallback to _create_or_update_shape: if POST fails → sticky ──────
OLD_SHAPE_METHOD_END = '''        # POST (create)
        new_id, op = await self._safe_request("post", base, payload, entity_type, entity_id, stats)
        if op == "created":
            stats["created"] += 1
        return new_id, op'''

NEW_SHAPE_METHOD_END = '''        # POST (create)
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
        return new_id, op'''

if OLD_SHAPE_METHOD_END in content:
    content = content.replace(OLD_SHAPE_METHOD_END, NEW_SHAPE_METHOD_END, 1)
    print("OK: added shape fallback to sticky_note")
else:
    # Try CRLF
    old_crlf = OLD_SHAPE_METHOD_END.replace('\n', '\r\n')
    if old_crlf in content:
        content = content.replace(old_crlf, NEW_SHAPE_METHOD_END, 1)
        print("OK: added shape fallback (CRLF)")
    else:
        print("WARN: shape method end not found")

# ── 3. Add _hex_to_sticky helper after _safe_content ─────────────────────────
OLD_AFTER_SAFE_CONTENT = '''# ── Stable entity IDs for sections'''
NEW_AFTER_SAFE_CONTENT = '''def _hex_to_sticky(hex_color: str) -> str:
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


# ── Stable entity IDs for sections'''

if OLD_AFTER_SAFE_CONTENT in content:
    content = content.replace(OLD_AFTER_SAFE_CONTENT, NEW_AFTER_SAFE_CONTENT, 1)
    print("OK: added _hex_to_sticky helper")
else:
    print("WARN: _hex_to_sticky insertion point not found")

# ── 4. Wrap ALL sync_all sections in try/except ───────────────────────────────
OLD_SYNC_BODY = '''        await self._render_board_header(session, user_id, now, stats)
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

        return stats'''

NEW_SYNC_BODY = '''        failed_sections = []

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
        await _safe_section("future",
            self._render_future_sections(session, user_id, time_service, stats))
        await _safe_section("exams",
            self._render_exams_section(session, user_id, time_service, stats))
        await _safe_section("study_schedule",
            self._render_study_schedule_section(session, user_id, time_service, stats))
        await _safe_section("study_blocks",
            self._render_study_blocks_section(session, user_id, time_service, stats))

        stats["failed_sections"] = failed_sections
        return stats'''

if OLD_SYNC_BODY in content:
    content = content.replace(OLD_SYNC_BODY, NEW_SYNC_BODY, 1)
    print("OK: wrapped all sync sections in try/except")
else:
    old_crlf = OLD_SYNC_BODY.replace('\n', '\r\n')
    if old_crlf in content:
        content = content.replace(old_crlf, NEW_SYNC_BODY, 1)
        print("OK: wrapped all sync sections (CRLF)")
    else:
        print("WARN: sync_all body not found")

# ── 5. Add _render_next_section_safe (catches NextStepService errors) ────────
OLD_NEXT_SECTION = '''    async def _render_next_section(self, session, user_id: int, time_service, next_step_service, stats: dict) -> None:
        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "next", t_str, stats)

        payload = await next_step_service.build_next_step(user_id, session, now)
        actions = payload.get("actions", [])[:3]
        mode = payload.get("mode", "normal")

        if not actions:
            body = "нет активных действий"
        else:
            body = "\\n".join(f"{i+1}. {a}" for i, a in enumerate(actions))

        content = f"СЛЕДУЮЩИЙ ШАГ\\n\\nрежим: {mode}\\n\\n{body}"
        x, y = self._card_xy("next", 0)
        mp = await self._get_mapping(session, user_id, "miro_next_card", _SECTION_ENTITY_IDS["next_card"])
        new_id, _ = await self._create_or_update_sticky(
            mp.item_id, content, x, y, "light_blue", "miro_next_card",
            _SECTION_ENTITY_IDS["next_card"], stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        stats["cards"] += 1'''

NEW_NEXT_SECTION = '''    async def _render_next_section(self, session, user_id: int, time_service, next_step_service, stats: dict) -> None:
        now = time_service.now()
        t_str = now.strftime("%H:%M")
        await self._render_section_header(session, user_id, "next", t_str, stats)

        payload = await next_step_service.build_next_step(user_id, session, now)
        actions = payload.get("actions", [])[:3]
        mode = payload.get("mode", "normal")

        if not actions:
            body = "нет активных действий"
        else:
            body = "\\n".join(f"{i+1}. {a}" for i, a in enumerate(actions))

        content = f"СЛЕДУЮЩИЙ ШАГ\\n\\nрежим: {mode}\\n\\n{body}"
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
            body = "\\n".join(f"{i+1}. {a}" for i, a in enumerate(actions)) if actions else "нет активных действий"
        except Exception as _exc:
            logger.exception("NextStepService.build_next_step failed: %s", _exc)
            body = "Следующий шаг временно недоступен"
            mode = "error"

        content = f"СЛЕДУЮЩИЙ ШАГ\\n\\nрежим: {mode}\\n\\n{body}"
        x, y = self._card_xy("next", 0)
        mp = await self._get_mapping(session, user_id, "miro_next_card", _SECTION_ENTITY_IDS["next_card"])
        new_id, _ = await self._create_or_update_sticky(
            mp.item_id, content, x, y, "light_blue", "miro_next_card",
            _SECTION_ENTITY_IDS["next_card"], stats,
        )
        await self._save_mapping(mp, new_id or mp.item_id, x, y)
        stats["cards"] += 1'''

if OLD_NEXT_SECTION in content:
    content = content.replace(OLD_NEXT_SECTION, NEW_NEXT_SECTION, 1)
    print("OK: added _render_next_section_safe")
else:
    old_crlf = OLD_NEXT_SECTION.replace('\n', '\r\n')
    if old_crlf in content:
        content = content.replace(old_crlf, NEW_NEXT_SECTION, 1)
        print("OK: added _render_next_section_safe (CRLF)")
    else:
        print("WARN: _render_next_section not found")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done.")
