#!/usr/bin/env python3
"""Patch health.py: add /dedupe_study command."""

path = "bot/handlers/health.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

# 1. Extend imports
OLD_IMPORTS = """from bot.database.queries import (
    get_active_problem_blocks,
    get_active_reminders,
    get_active_tasks,
    get_all_exam_dates,
    get_all_study_schedule,
    get_archived_tasks,
    get_or_create_runtime_state,
    get_upcoming_overrides,
)"""

NEW_IMPORTS = """from bot.database.queries import (
    get_active_problem_blocks,
    get_active_reminders,
    get_active_tasks,
    get_all_exam_dates,
    get_all_study_schedule,
    get_archived_tasks,
    get_or_create_runtime_state,
    get_upcoming_overrides,
    find_duplicate_exams,
    archive_exam_date,
    get_exam_date_by_id,
)"""

if OLD_IMPORTS in content:
    content = content.replace(OLD_IMPORTS, NEW_IMPORTS, 1)
    print("OK: extended health.py imports")
else:
    print("WARN: health.py imports block not found")

# 2. Append dedupe_study command
DEDUPE_HANDLER = '''

@router.message(Command("dedupe_study"))
async def dedupe_study_cmd(message: Message, session_factory, config, screen_service):
    """
    Find duplicate exam_dates and offer inline buttons to archive duplicates.
    Only accessible to ALLOWED_USER_ID.
    """
    if message.from_user.id != config.allowed_user_id:
        return

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    try:
        async with session_factory() as session:
            pairs = await find_duplicate_exams(session, message.from_user.id)
            await session.commit()
    except Exception as exc:
        await message.answer(f"Ошибка при поиске дублей: {exc}")
        await screen_service.delete_user_input(message)
        return

    if not pairs:
        await message.answer(
            "/dedupe_study\\n\\nДублей не найдено.\\n\\n"
            "Все предметы имеют уникальные канонические имена."
        )
        await screen_service.delete_user_input(message)
        return

    lines = ["/dedupe_study\\n", f"Найдено пар дублей: {len(pairs)}\\n"]
    keyboards = []

    for primary, dup in pairs:
        lines.append(f"Оставить: {primary.subject} ({primary.exam_date})")
        lines.append(f"Убрать:   {dup.subject} ({dup.exam_date})")
        lines.append("")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"Убрать «{dup.subject}»",
                callback_data=f"dedupe_archive:{dup.id}:{primary.id}",
            ),
            InlineKeyboardButton(
                text="Оставить оба",
                callback_data=f"dedupe_keep:{dup.id}",
            ),
        ]])
        keyboards.append((f"Дубль: {dup.subject} → {primary.subject}", kb))

    await message.answer("\\n".join(lines))
    for text, kb in keyboards:
        await message.answer(text, reply_markup=kb)

    await screen_service.delete_user_input(message)
'''

if "dedupe_study_cmd" not in content:
    content = content.rstrip() + "\n" + DEDUPE_HANDLER
    print("OK: added /dedupe_study command")
else:
    print("INFO: /dedupe_study already present")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done.")
