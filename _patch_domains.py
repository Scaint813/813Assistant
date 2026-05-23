#!/usr/bin/env python3
"""Patch domains.py:
1. Add new imports (upsert_exam_date_smart, archive_exam_date, archive_study_schedule_item)
2. Add update_exam_date, delete_exam_date, delete_study_schedule_item handlers in confirm_preview
3. Fix result_text to show created/updated counts + total_active
4. Add dedupe_archive callback handler
"""

path = "bot/handlers/domains.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

# ── 1. Extend imports ─────────────────────────────────────────────────────────
OLD_IMPORTS = """from bot.database.queries import (
    create_exam_date,
    create_reminder,
    create_schedule_override,
    create_study_schedule_item,
    create_task,
    find_task_by_title,
    get_active_tasks,
    get_pending_preview,
    get_or_create_runtime_state,
    get_reminder_by_id,
    get_problem_block_by_id,
)"""

NEW_IMPORTS = """from bot.database.queries import (
    create_exam_date,
    create_reminder,
    create_schedule_override,
    create_study_schedule_item,
    create_task,
    find_task_by_title,
    get_active_tasks,
    get_active_exam_dates,
    get_active_study_schedule,
    get_pending_preview,
    get_or_create_runtime_state,
    get_reminder_by_id,
    get_problem_block_by_id,
    upsert_exam_date_smart,
    archive_exam_date,
    archive_study_schedule_item,
)
from bot.services.subject_normalizer import normalize_subject, find_best_exam_match"""

if OLD_IMPORTS in content:
    content = content.replace(OLD_IMPORTS, NEW_IMPORTS, 1)
    print("OK: extended imports")
else:
    print("WARN: imports block not found")

# ── 2. Replace set_exam_date handler with upsert_exam_date_smart ─────────────
OLD_SET_EXAM = """            elif t == \"set_exam_date\":
                subject = intent.get(\"subject\") or \"Предмет\"
                exam_date_str = intent.get(\"exam_date\") or \"\"
                if exam_date_str:
                    from datetime import date as _date
                    try:
                        exam_date = _date.fromisoformat(exam_date_str)
                        await create_exam_date(session, callback.from_user.id, subject, exam_date)
                    except ValueError:
                        import logging
                        logging.getLogger(__name__).warning(\"Invalid exam_date: %s\", exam_date_str)"""

NEW_SET_EXAM = """            elif t == \"set_exam_date\":
                subject = normalize_subject(intent.get(\"subject\") or \"Предмет\")
                exam_date_str = intent.get(\"exam_date\") or \"\"
                if exam_date_str:
                    from datetime import date as _date
                    try:
                        exam_date = _date.fromisoformat(exam_date_str)
                        _, op = await upsert_exam_date_smart(session, callback.from_user.id, subject, exam_date)
                        if op == \"created\":
                            study_created_exams += 1
                        else:
                            study_updated_exams.append(subject)
                    except ValueError:
                        import logging
                        logging.getLogger(__name__).warning(\"Invalid exam_date: %s\", exam_date_str)

            elif t == \"update_exam_date\":
                target_subject = normalize_subject(intent.get(\"target_subject\") or intent.get(\"subject\") or \"\")
                new_date_str = intent.get(\"new_date\") or intent.get(\"exam_date\") or \"\"
                if target_subject and new_date_str:
                    from datetime import date as _date
                    try:
                        new_date = _date.fromisoformat(new_date_str)
                        # Find existing exam by canonical subject
                        res_exams = await get_active_exam_dates(session, callback.from_user.id)
                        all_exams = await session.execute(__import__('sqlalchemy').select(__import__('bot.database.models', fromlist=['ExamDate']).ExamDate).where(__import__('bot.database.models', fromlist=['ExamDate']).ExamDate.user_id == callback.from_user.id))
                        target_exam = find_best_exam_match(res_exams, target_subject)
                        if target_exam:
                            target_exam.exam_date = new_date
                            target_exam.status = \"active\"
                            await session.flush()
                            study_updated_exams.append(target_subject)
                        else:
                            # Fallback: create as new
                            await upsert_exam_date_smart(session, callback.from_user.id, target_subject, new_date)
                            study_created_exams += 1
                    except Exception as exc:
                        import logging
                        logging.getLogger(__name__).warning(\"update_exam_date failed: %s\", exc)

            elif t == \"delete_exam_date\":
                target_subject = normalize_subject(intent.get(\"target_subject\") or intent.get(\"subject\") or \"\")
                if target_subject:
                    from sqlalchemy import select as _sel
                    from bot.database.models import ExamDate as _ExamDate
                    res_all = await session.execute(
                        _sel(_ExamDate).where(_ExamDate.user_id == callback.from_user.id, _ExamDate.status == \"active\")
                    )
                    all_active = list(res_all.scalars().all())
                    target_exam = find_best_exam_match(all_active, target_subject)
                    if target_exam:
                        await archive_exam_date(session, callback.from_user.id, target_exam.id, \"deleted by user\")
                        study_deleted_exams.append(target_subject)

            elif t == \"delete_study_schedule_item\":
                target_subject = normalize_subject(intent.get(\"target_subject\") or intent.get(\"subject\") or \"\")
                target_weekday = intent.get(\"weekday\") or \"\"
                if target_subject:
                    from sqlalchemy import select as _sel, and_ as _and
                    from bot.database.models import StudyScheduleItem as _SSI
                    q = _sel(_SSI).where(_and(_SSI.user_id == callback.from_user.id, _SSI.status == \"active\"))
                    res_sched = await session.execute(q)
                    items = list(res_sched.scalars().all())
                    norm_target = normalize_subject(target_subject)
                    for item in items:
                        if normalize_subject(item.subject) == norm_target:
                            if not target_weekday or item.weekday == target_weekday:
                                await archive_study_schedule_item(session, callback.from_user.id, item.id, \"deleted by user\")
                                study_deleted_schedule.append(f\"{item.subject} {item.weekday}\")
                                break"""

if OLD_SET_EXAM in content:
    content = content.replace(OLD_SET_EXAM, NEW_SET_EXAM, 1)
    print("OK: replaced set_exam_date with smart upsert + added update/delete handlers")
else:
    print("WARN: set_exam_date block not found")

# ── 3. Add tracking variables before the intent loop ─────────────────────────
OLD_BEFORE_LOOP = """        created_tasks_by_title: dict = {}
        created_reminders = []
        created_problem_blocks = []"""

NEW_BEFORE_LOOP = """        created_tasks_by_title: dict = {}
        created_reminders = []
        created_problem_blocks = []
        study_created_exams: int = 0
        study_updated_exams: list = []
        study_deleted_exams: list = []
        study_deleted_schedule: list = []"""

if OLD_BEFORE_LOOP in content:
    content = content.replace(OLD_BEFORE_LOOP, NEW_BEFORE_LOOP, 1)
    print("OK: added study tracking variables")
else:
    print("WARN: tracking vars block not found")

# ── 4. Fix result_text for study operations ───────────────────────────────────
OLD_RESULT = """    intents_types = {i.get(\"type\") for i in payload.get(\"intents\", [])}
    has_study = bool(intents_types & {\"set_exam_date\", \"create_study_schedule_item\"})
    if has_study or created_problem_blocks:
        parts = []
        if has_study:
            n_exams = sum(1 for i in payload.get(\"intents\", []) if i.get(\"type\") == \"set_exam_date\")
            n_sched = sum(1 for i in payload.get(\"intents\", []) if i.get(\"type\") == \"create_study_schedule_item\")
            if n_exams:
                parts.append(f\"экзаменов: {n_exams}\")
            if n_sched:
                parts.append(f\"занятий: {n_sched}\")
        if created_problem_blocks:
            parts.append(f\"блоков: {len(created_problem_blocks)}\")
        result_text = \"Учебный контур сохранён. \" + \", \".join(parts) + \".\\n\\nСмотри: /study\""""

NEW_RESULT = """    intents_types = {i.get(\"type\") for i in payload.get(\"intents\", [])}
    has_study = bool(intents_types & {\"set_exam_date\", \"create_study_schedule_item\",
                                      \"update_exam_date\", \"delete_exam_date\", \"delete_study_schedule_item\"})
    if has_study or created_problem_blocks:
        # Build detailed result with created/updated/total counts
        result_lines = [\"Готово.\\n\"]

        # Exam changes
        if study_created_exams > 0:
            result_lines.append(f\"Добавлено экзаменов: {study_created_exams}\")
        if study_updated_exams:
            result_lines.append(\"Обновлено:\")
            for subj in study_updated_exams:
                result_lines.append(f\"  • {subj}\")
        if study_deleted_exams:
            result_lines.append(\"Убрано:\")
            for subj in study_deleted_exams:
                result_lines.append(f\"  • {subj}\")
        if study_deleted_schedule:
            result_lines.append(\"Занятий убрано:\")
            for s in study_deleted_schedule:
                result_lines.append(f\"  • {s}\")

        # Schedule creates
        n_sched = sum(1 for i in payload.get(\"intents\", []) if i.get(\"type\") == \"create_study_schedule_item\")
        if n_sched:
            result_lines.append(f\"Занятий добавлено: {n_sched}\")

        # Problem blocks
        if created_problem_blocks:
            result_lines.append(f\"Блоков: {len(created_problem_blocks)}\")
            for b in created_problem_blocks[:2]:
                result_lines.append(f\"  • {b.title}\")
                result_lines.append(f\"    Шаг: {b.next_action[:60]}\")
                result_lines.append(\"    Подробный план — в Miro после /sync_miro\")

        # Totals (fetched fresh from DB)
        try:
            async with session_factory() as _s:
                _active_exams = await get_active_exam_dates(_s, callback.from_user.id)
                _active_sched = await get_active_study_schedule(_s, callback.from_user.id)
            result_lines.append(\"\")
            result_lines.append(f\"Активных экзаменов: {len(_active_exams)}\")
            result_lines.append(f\"Занятий: {len(_active_sched)}\")
        except Exception:
            pass

        result_lines.append(\"\\nСмотри: /study\")
        result_text = \"\\n\".join(result_lines)"""

if OLD_RESULT in content:
    content = content.replace(OLD_RESULT, NEW_RESULT, 1)
    print("OK: fixed result_text with created/updated/total")
else:
    print("WARN: result_text block not found")

# ── 5. Add dedupe_archive callback handler (append before end of file) ────────
DEDUPE_CALLBACK = '''

# ── Dedupe study callback ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dedupe_archive:"))
async def dedupe_archive_callback(callback: CallbackQuery, session_factory, config):
    """Handle dedupe confirmation: archive the duplicate exam_date."""
    await callback.answer()
    if callback.from_user.id != config.allowed_user_id:
        return
    parts = callback.data.split(":")
    # dedupe_archive:{duplicate_id}:{keep_id}
    if len(parts) < 3:
        await callback.message.edit_text("Ошибка: неверный формат.", reply_markup=None)
        return
    dup_id = int(parts[1])
    keep_id = int(parts[2])
    async with session_factory() as session:
        dup = await archive_exam_date(session, callback.from_user.id, dup_id, "deduplicated")
        from bot.database.queries import get_exam_date_by_id
        kept = await get_exam_date_by_id(session, callback.from_user.id, keep_id)
        await session.commit()
    if dup:
        kept_subj = kept.subject if kept else "?"
        await callback.message.edit_text(
            f"Дубль убран: {dup.subject}\\nОстаётся: {kept_subj}\\n\\nПроверь: /study",
            reply_markup=None,
        )
    else:
        await callback.message.edit_text("Экзамен не найден.", reply_markup=None)


@router.callback_query(F.data.startswith("dedupe_keep:"))
async def dedupe_keep_callback(callback: CallbackQuery):
    """Handle dedupe skip: keep both exams."""
    await callback.answer()
    await callback.message.edit_text("Оставляю оба. Если нужно — удали вручную через /dedupe_study.", reply_markup=None)
'''

if "dedupe_archive_callback" not in content:
    content = content.rstrip() + "\n" + DEDUPE_CALLBACK
    print("OK: added dedupe_archive callback handler")
else:
    print("INFO: dedupe_archive callback already present")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("domains.py patch complete.")
