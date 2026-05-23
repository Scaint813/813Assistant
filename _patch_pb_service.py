#!/usr/bin/env python3
"""Patch problem_block_service.py to integrate study_problem_templates."""

path = "bot/services/problem_block_service.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

# 1. Add import for study_problem_templates
OLD_IMPORT = "from bot.database.queries import add_problem_event, create_problem_block, get_active_problem_blocks, get_problem_block_by_id"
NEW_IMPORT = """from bot.database.queries import add_problem_event, create_problem_block, get_active_problem_blocks, get_problem_block_by_id
from bot.services.study_problem_templates import build_study_problem_plan"""

if OLD_IMPORT in content:
    content = content.replace(OLD_IMPORT, NEW_IMPORT, 1)
    print("OK: added study_problem_templates import")
else:
    print("WARN: import line not found")

# 2. Replace the create_problem_block method to use templates for exam/study categories
OLD_CREATE = '''    async def create_problem_block(self, user_id: int, intent: dict, session, now: datetime, source_type: str = "text", resources: list[dict] | None = None):
        category = intent.get("category") if intent.get("category") in self.VALID_CATEGORIES else "other"
        deadline = datetime.fromisoformat(intent["deadline"]) if intent.get("deadline") else None
        if category == "exam" and not deadline and now.date().isoformat() <= "2026-06-18":
            deadline = datetime.fromisoformat("2026-06-18T23:59:00+00:00")
        review_delta = timedelta(hours=6 if self._norm_priority(intent.get("priority")) in {"high", "urgent"} else 24)
        block = await create_problem_block(
            session,
            user_id,
            category=category,
            title=intent.get("title") or "Активный блок",
            description=intent.get("description") or "",
            problem_text=intent.get("problem_text") or "",
            solution_strategy=intent.get("solution_strategy") or "",
            next_action=intent.get("next_action") or "Сделать первый короткий шаг.",
            status="active",
            priority=self._norm_priority(intent.get("priority")),
            pressure_level=self._norm_pressure(intent.get("pressure_level")),
            deadline=deadline,
            next_review_at=now + review_delta,
            source_type=source_type,
            resources_json=json.dumps(resources or [], ensure_ascii=False),
        )
        await add_problem_event(session, user_id, block.id, "created", block.title)
        return block'''

NEW_CREATE = '''    async def create_problem_block(self, user_id: int, intent: dict, session, now: datetime, source_type: str = "text", resources: list[dict] | None = None):
        category = intent.get("category") if intent.get("category") in self.VALID_CATEGORIES else "other"
        deadline = datetime.fromisoformat(intent["deadline"]) if intent.get("deadline") else None
        if category == "exam" and not deadline and now.date().isoformat() <= "2026-06-18":
            deadline = datetime.fromisoformat("2026-06-18T23:59:00+00:00")
        review_delta = timedelta(hours=6 if self._norm_priority(intent.get("priority")) in {"high", "urgent"} else 24)

        # ── Enrich exam/study blocks with curated templates ───────────────────
        title = intent.get("title") or "Активный блок"
        problem_text = intent.get("problem_text") or ""
        solution_strategy = intent.get("solution_strategy") or ""
        next_action = intent.get("next_action") or "Сделать первый короткий шаг."

        if category in {"exam", "study"}:
            subject = intent.get("subject") or ""
            try:
                plan = build_study_problem_plan(subject, title, problem_text)
                # Override with template values if they are more specific
                if plan.get("next_action"):
                    next_action = plan["next_action"]
                if plan.get("theory_topics"):
                    theory = "\\n".join(f"{i}. {t}" for i, t in enumerate(plan["theory_topics"], 1))
                    solution_strategy = f"Темы для повторения:\\n{theory}"
                # Merge miro_plan into resources_json
                merged_resources = list(resources or [])
                merged_resources.append({"type": "study_plan", "plan": plan.get("miro_plan", {})})
                resources = merged_resources
            except Exception as _exc:
                import logging
                logging.getLogger(__name__).warning("study_problem_templates failed: %s", _exc)

        block = await create_problem_block(
            session,
            user_id,
            category=category,
            title=title,
            description=intent.get("description") or "",
            problem_text=problem_text,
            solution_strategy=solution_strategy,
            next_action=next_action,
            status="active",
            priority=self._norm_priority(intent.get("priority")),
            pressure_level=self._norm_pressure(intent.get("pressure_level")),
            deadline=deadline,
            next_review_at=now + review_delta,
            source_type=source_type,
            resources_json=json.dumps(resources or [], ensure_ascii=False),
        )
        await add_problem_event(session, user_id, block.id, "created", block.title)
        return block'''

if OLD_CREATE in content:
    content = content.replace(OLD_CREATE, NEW_CREATE, 1)
    print("OK: enriched create_problem_block with study templates")
else:
    print("WARN: create_problem_block method not found")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done.")
