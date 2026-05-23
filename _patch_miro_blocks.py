#!/usr/bin/env python3
"""Patch miro_service.py: replace _render_study_blocks_section with 4-sticky mini-structure per block."""

path = "bot/services/miro_service.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

OLD_BLOCKS_METHOD = '''    async def _render_study_blocks_section(self, session, user_id: int, time_service, stats: dict) -> None:
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
            stats["cards"] += 1'''

NEW_BLOCKS_METHOD = '''    async def _render_study_blocks_section(self, session, user_id: int, time_service, stats: dict) -> None:
        """
        Render study problem blocks section.
        For each block: 4 sticky notes side by side (ПРОБЛЕМА | ТЕМЫ | ПРАКТИКА | СЛЕДУЮЩИЙ ШАГ).
        Each block occupies one column position. sticky offset: block_col * 4 within section.
        Uses stable entity IDs: block.id * 10 + sub_card_offset (0-3).
        """
        import json as _json
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

        # Sub-card layout: for each block, render 4 stickies side-by-side
        # CARD_W = section column width / 4  (each sub-card is 1/4 of column)
        # We use synthetic col positions: block_i * 4 + sub_offset
        SECTION_KEY = "study_blocks"
        COL_W = self._col_width    # base column width in px
        CARD_H = self._card_height  # base card height in px
        BASE_X, BASE_Y = self._section_xy(SECTION_KEY)
        BASE_Y += CARD_H + 20       # below header
        SUB_W = max(200, COL_W // 4)  # each sub-card width

        SUB_CARDS = [
            # (entity_id_offset, color, label_fn, content_fn)
            (0, "red",         lambda b: "ПРОБЛЕМА",
             lambda b: f"{b.title}\\n\\n{(b.problem_text or b.title)[:120]}"),
            (1, "light_blue",  lambda b: "ТЕМЫ",
             lambda b: _extract_theory(b)),
            (2, "light_green", lambda b: "ПРАКТИКА",
             lambda b: _extract_practice(b)),
            (3, "yellow",      lambda b: "СЛЕДУЮЩИЙ ШАГ",
             lambda b: (b.next_action or "—")[:200]),
        ]

        def _extract_theory(block) -> str:
            try:
                resources = _json.loads(block.resources_json or "[]")
                for r in resources:
                    if isinstance(r, dict) and r.get("type") == "study_plan":
                        plan = r.get("plan", {})
                        if plan.get("theory"):
                            return "ТЕМЫ:\\n" + plan["theory"][:300]
            except Exception:
                pass
            strat = block.solution_strategy or ""
            if strat:
                return strat[:200]
            return "Темы: см. спецификацию ЕГЭ"

        def _extract_practice(block) -> str:
            try:
                resources = _json.loads(block.resources_json or "[]")
                for r in resources:
                    if isinstance(r, dict) and r.get("type") == "study_plan":
                        plan = r.get("plan", {})
                        if plan.get("practice"):
                            return "ПРАКТИКА:\\n" + plan["practice"][:300]
            except Exception:
                pass
            return "1. Решить 5 типовых заданий\\n2. Разобрать ошибки\\n3. Повтор через 2 дня"

        for block_i, b in enumerate(blocks[:MAX_PROBLEMS]):
            dl = b.deadline.strftime("%d.%m.%Y") if b.deadline else "—"

            for sub_offset, color, label_fn, content_fn in SUB_CARDS:
                try:
                    content_text = content_fn(b)
                except Exception:
                    content_text = "—"
                label_text = label_fn(b)
                full_content = f"{label_text}\\n\\n{content_text}"

                # Position: each block = 4 sub-cards in row; blocks stacked vertically
                sub_x = BASE_X + block_i * COL_W + sub_offset * SUB_W
                sub_y = BASE_Y

                entity_id = b.id * 10 + sub_offset
                entity_type = f"study_block_sub"
                mp = await self._get_mapping(session, user_id, entity_type, entity_id)
                new_id, _ = await self._create_or_update_sticky(
                    mp.item_id, full_content, sub_x, sub_y, color,
                    entity_type, entity_id, stats,
                )
                await self._save_mapping(mp, new_id or mp.item_id, sub_x, sub_y)
                stats["cards"] += 1'''

if OLD_BLOCKS_METHOD in content:
    content = content.replace(OLD_BLOCKS_METHOD, NEW_BLOCKS_METHOD, 1)
    print("OK: replaced _render_study_blocks_section with 4-sticky mini-structure")
else:
    print("WARN: method not found, trying CRLF variant")
    OLD_CRLF = OLD_BLOCKS_METHOD.replace('\n', '\r\n')
    if OLD_CRLF in content:
        content = content.replace(OLD_CRLF, NEW_BLOCKS_METHOD, 1)
        print("OK: replaced (CRLF)")
    else:
        print("FAIL: could not find method")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done.")
